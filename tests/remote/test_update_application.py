"""Applying a remote-triggered update, and the one branch that must not run.

`_apply_git_update` handles MSG_GIT_UPDATE: the admin console's Update button
reaching a client. It fetches, force-checks-out, pip-installs and then
restarts the process. It was at 16.5% coverage -- the least-tested module in
the remote package, and the one that ends in a hard `os._exit`.

The property that matters more than the rest: **a failed update must not
restart.** `apply_update()` returning `ok=False` means the working tree is in
whatever state the failure left it. Restarting into that would come back up on
a half-applied checkout, and -- because the admin console's Update button can
be pressed again -- do it repeatedly. The guard is a single `if not
result.get("ok"): return`, and nothing was checking it was there.

No process is ever restarted here: `_do_restart` and `os._exit` are stubbed in
every test, and the failure tests assert the stub was never reached.

`_refresh_desktop_icon` is Windows-only cosmetics, but it shells out to
PowerShell during an update, so its "give up quietly" contract is worth
pinning: an update must not be derailed by a missing shortcut.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from backend.src.services.cluster.remote import _update
from backend.src.services.positions import core_app_update


@pytest.fixture
def restarts(monkeypatch):
    """Records restart attempts instead of performing them."""
    calls: list = []
    monkeypatch.setattr(_update, "_do_restart", lambda: calls.append("restart"))
    return calls


@pytest.fixture
def update_result(monkeypatch):
    """Drives what apply_update() returns, and records how it was called."""
    box: dict = {"value": {"ok": True}, "calls": []}

    async def _fake(restart=True):
        box["calls"].append(restart)
        return box["value"]
    monkeypatch.setattr(core_app_update, "apply_update", _fake)
    return box


@pytest.fixture
def no_icon_refresh(monkeypatch):
    seen: list = []
    monkeypatch.setattr(_update, "_refresh_desktop_icon", lambda root: seen.append(root))
    return seen


class TestAFailedUpdateDoesNotRestart:
    """The guard worth having a test for."""

    def test_ok_false_does_not_restart(self, restarts, update_result,
                                       no_icon_refresh):
        update_result["value"] = {"ok": False, "error": "git checkout failed"}

        asyncio.run(_update._apply_git_update())

        assert restarts == [], (
            "restarted after a failed update -- the app would come back up on "
            "a half-applied checkout"
        )

    def test_a_missing_ok_key_is_treated_as_failure(self, restarts,
                                                    update_result,
                                                    no_icon_refresh):
        """`.get("ok")` on a result shape nobody expected must fail closed."""
        update_result["value"] = {"error": "something odd"}

        asyncio.run(_update._apply_git_update())

        assert restarts == []

    def test_an_empty_result_is_treated_as_failure(self, restarts,
                                                   update_result,
                                                   no_icon_refresh):
        update_result["value"] = {}

        asyncio.run(_update._apply_git_update())

        assert restarts == []

    def test_a_failed_update_does_not_touch_the_desktop_shortcut_either(
            self, restarts, update_result, no_icon_refresh, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        update_result["value"] = {"ok": False, "error": "nope"}

        asyncio.run(_update._apply_git_update())

        assert no_icon_refresh == []


def test_apply_update_is_told_not_to_restart_itself(restarts, update_result,
                                                     no_icon_refresh):
    """apply_update() restarts on success by default (2026-09-03) -- this
    path must opt out with restart=False, since it runs its own restart
    sequence (icon refresh, then _do_restart) right after. Losing this flag
    would restart twice: once inside apply_update(), pre-empting the icon
    refresh and the bat-loop's exit-code-42 relaunch entirely."""
    asyncio.run(_update._apply_git_update())

    assert update_result["calls"] == [False]


class TestASuccessfulUpdateDoesRestart:
    """Positive control. Without it, a version that never restarted would pass
    every test above."""

    def test_it_restarts_on_a_non_windows_platform(self, restarts,
                                                   update_result, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        update_result["value"] = {"ok": True}

        asyncio.run(_update._apply_git_update())

        assert restarts == ["restart"]

    def test_on_windows_it_refreshes_the_icon_before_restarting(
            self, restarts, update_result, no_icon_refresh, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        update_result["value"] = {"ok": True}

        asyncio.run(_update._apply_git_update())

        assert len(no_icon_refresh) == 1
        assert restarts == ["restart"], (
            "the fresh-client load is expected to fail in the test tree, so "
            "the documented fallback to the in-memory _do_restart must fire"
        )


class TestTheFreshClientLoadFallsBackRatherThanStranding:
    """On Windows the update re-loads the just-deployed client.py from disk so
    this push's restart logic takes effect immediately. If that load fails for
    any reason, it MUST fall back to the in-memory restart -- otherwise the
    update is applied and the app never comes back, which is the worst outcome
    of the three (worse than not updating, and worse than restarting old code).
    """

    def test_a_broken_fresh_load_still_restarts(self, restarts, update_result,
                                                no_icon_refresh, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        update_result["value"] = {"ok": True}

        import importlib.util as ilu

        def _explode(*_a, **_kw):
            raise OSError("client.py is not readable")
        monkeypatch.setattr(ilu, "spec_from_file_location", _explode)

        asyncio.run(_update._apply_git_update())

        assert restarts == ["restart"]


class TestTheIconRefreshGivesUpQuietly:
    """It runs in the middle of an update. Anything it raises would abort the
    restart and leave the app on new code that never came back up."""

    def test_no_icon_file_means_no_powershell(self, monkeypatch, tmp_path):
        ran: list = []
        monkeypatch.setattr(_update.subprocess, "run",
                            lambda *a, **kw: ran.append(a))

        _update._refresh_desktop_icon(tmp_path)      # no frontend/static/ at all

        assert ran == []

    def test_no_shortcut_means_no_powershell(self, monkeypatch, tmp_path):
        ico = tmp_path / "frontend" / "static"
        ico.mkdir(parents=True)
        (ico / "gold_bag.ico").write_bytes(b"\x00")
        ran: list = []
        monkeypatch.setattr(_update.subprocess, "run",
                            lambda *a, **kw: ran.append(a))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "nobody"))
        monkeypatch.setenv("PUBLIC", str(tmp_path / "nopublic"))

        _update._refresh_desktop_icon(tmp_path)

        assert ran == [], "shelled out to PowerShell with no shortcut to stamp"

    def test_a_powershell_failure_is_swallowed(self, monkeypatch, tmp_path):
        ico = tmp_path / "frontend" / "static"
        ico.mkdir(parents=True)
        (ico / "gold_bag.ico").write_bytes(b"\x00")
        desktop = tmp_path / "user" / "Desktop"
        desktop.mkdir(parents=True)
        (desktop / "FOREX Trader.lnk").write_bytes(b"\x00")
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
        monkeypatch.setenv("PUBLIC", str(tmp_path / "nopublic"))

        def _boom(*_a, **_kw):
            raise OSError("powershell is not on PATH")
        monkeypatch.setattr(_update.subprocess, "run", _boom)

        _update._refresh_desktop_icon(tmp_path)      # must not raise

    def test_it_stamps_a_shortcut_when_there_is_one(self, monkeypatch, tmp_path):
        """Positive control for the two 'no PowerShell' tests above."""
        ico = tmp_path / "frontend" / "static"
        ico.mkdir(parents=True)
        (ico / "gold_bag.ico").write_bytes(b"\x00")
        desktop = tmp_path / "user" / "Desktop"
        desktop.mkdir(parents=True)
        (desktop / "FOREX Trader.lnk").write_bytes(b"\x00")
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
        monkeypatch.setenv("PUBLIC", str(tmp_path / "nopublic"))
        ran: list = []
        monkeypatch.setattr(_update.subprocess, "run",
                            lambda *a, **kw: ran.append(a[0]))

        _update._refresh_desktop_icon(tmp_path)

        assert len(ran) == 1
        assert ran[0][0] == "powershell"
        assert "gold_bag.ico" in " ".join(ran[0])


class TestTheRestartExitCodeIsTheOneTheLauncherWatchesFor:
    def test_it_is_42(self):
        """The bat launcher's loop re-runs run.py only on this exact code.
        Changing it here without changing the launcher turns every restart
        into a silent shutdown."""
        assert _update._RESTART_EXIT_CODE == 42


# ─────────────────────────────────────────────────────────────────────────────
# The two functions the 2026-08-30 split broke
# ─────────────────────────────────────────────────────────────────────────────

class TestTheBeaconAndVersionHelpersActuallyRun:
    """Regression tests for a bug I introduced.

    The 2026-08-30 split moved `_read_changelog` and `_lan_beacon_loop` into
    `_beacon_version.py` without the two names they depend on
    (`_CHANGELOG_FILE`, `SERVER_PORT`), which stayed in `server.py`. Both
    raised NameError on first use, and `tools.checks all` was 8/8 throughout.

    `_read_changelog` is called on EVERY successful client connection -- the
    welcome sequence sends MSG_WELCOME, then the licence if there is one, then
    MSG_VERSION_INFO carrying this. So every client would have been welcomed,
    licensed, and then dropped before it learned what version to update to.

    The static gate that catches the whole class is
    tests/refactor/test_undefined_names.py. These two are here because a name
    resolving is not the same as a function working.
    """

    def test_reading_the_changelog_returns_lines(self):
        from backend.src.services.cluster.remote import _beacon_version as bv

        lines = bv._read_changelog()

        assert isinstance(lines, list)
        assert len(lines) <= 40, "the welcome payload is capped at 40 lines"

    def test_reading_the_changelog_with_no_file_returns_empty(self, monkeypatch,
                                                              tmp_path):
        from backend.src.services.cluster.remote import _beacon_version as bv
        monkeypatch.setattr(bv, "_repo_root_for_files", lambda: tmp_path)

        assert bv._read_changelog() == []

    def test_the_beacon_payload_carries_the_real_server_port(self, monkeypatch):
        """The whole point of the beacon: a LAN client learns where to connect.
        A wrong or missing port makes discovery worse than useless."""
        import asyncio
        import json

        from backend.src.services.cluster.remote import _beacon_version as bv
        from backend.src.services.cluster.remote import tls

        sent: list = []
        monkeypatch.setattr(bv, "_get_local_ip", lambda: "192.168.1.50")

        async def _one_iteration():
            task = asyncio.create_task(bv._lan_beacon_loop())
            await asyncio.sleep(0)          # let it reach the first broadcast
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        async def _run():
            loop = asyncio.get_running_loop()

            async def _capture(_ex, fn, payload):
                sent.append(json.loads(payload))
            monkeypatch.setattr(loop, "run_in_executor", _capture)
            await _one_iteration()

        asyncio.run(_run())

        assert sent, "the beacon never broadcast anything"
        assert sent[0]["port"] == tls.SERVER_PORT
        assert sent[0]["ip"] == "192.168.1.50"
        assert sent[0]["type"] == "forex_admin_beacon"
