"""OS-level auto-restart supervision.

`core_autostart.py` was 66% covered, with the platform installers and the
public API almost entirely untested.

This is the code that came out of a real outage: a clean shutdown on
2026-08-14 19:09 left the app down for roughly 47 hours -- there is no
`forex_trader.log.2026-08-15` at all -- until someone logged in remotely and
started it by hand. A crash or a VPS reboot would have looked identical.

So the properties worth pinning are the ones that decide whether the app comes
back: that `enable()` refuses loudly when it cannot really install, that
`disable()` disarms even when the uninstall fails, and that `sync_from_setting`
can never raise -- a supervision feature that blocks boot is worse than no
supervision at all.

No scheduler is touched. `_launchctl` and `_schtasks` are replaced with
recorders, so nothing is registered with launchd or Task Scheduler, and the
armed flag is redirected into tmp_path.
"""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from backend.src.services.positions import core_autostart as au


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect every file the module writes, and record scheduler calls."""
    monkeypatch.setattr(au, "ARMED_FLAG", tmp_path / "armed", raising=False)
    monkeypatch.setattr(au, "LAST_LAUNCH_FILE", tmp_path / "last", raising=False)
    monkeypatch.setattr(au, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log", raising=False)
    monkeypatch.setattr(au, "_PLIST_PATH", tmp_path / "agents" / "forex.plist", raising=False)

    calls = {"launchctl": [], "schtasks": []}

    def _lc(*args):
        calls["launchctl"].append(args)
        return _completed()

    def _st(*args):
        calls["schtasks"].append(args)
        return _completed()

    monkeypatch.setattr(au, "_launchctl", _lc)
    monkeypatch.setattr(au, "_schtasks", _st)

    # These tests simulate a platform by patching au.sys.platform rather than
    # by skipping, so the macOS path is exercised on every host -- which is the
    # right call, and the reason it must be simulated COMPLETELY. os.getuid is
    # POSIX-only: on a Windows runner the darwin tests reached _mac_install()
    # and died with "module 'os' has no attribute 'getuid'". Four failures in
    # CI run 33111402669, all this one line.
    #
    # raising=False because on Windows there is no attribute to replace.
    monkeypatch.setattr(au.os, "getuid", lambda: 501, raising=False)

    return types.SimpleNamespace(calls=calls, tmp=tmp_path)


@pytest.fixture
def watchdog_present(monkeypatch, tmp_path):
    script = tmp_path / "watchdog.py"
    script.write_text("# watchdog\n", encoding="utf-8")
    monkeypatch.setattr(au, "watchdog_script", lambda: script)
    return script


# ── The armed flag ────────────────────────────────────────────────────────────

def test_arming_and_disarming_are_visible_to_the_watchdog(sandbox):
    assert au.is_armed() is False
    au.arm()
    assert au.is_armed() is True
    au.disarm()
    assert au.is_armed() is False


def test_disarming_clears_the_last_launch_stamp(sandbox):
    """A stale timestamp would make the next arm() sit out its first tick for
    no reason -- and the first tick is the one that matters after a reboot."""
    au.arm()
    au.LAST_LAUNCH_FILE.write_text("12345", encoding="utf-8")

    au.disarm()

    assert not au.LAST_LAUNCH_FILE.exists()


def test_disarming_twice_is_not_an_error(sandbox):
    au.disarm()
    au.disarm()


# ── enable() ──────────────────────────────────────────────────────────────────

def test_enabling_refuses_on_an_unsupported_platform(sandbox, monkeypatch):
    monkeypatch.setattr(au.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="not supported"):
        au.enable()
    assert au.is_armed() is False, "nothing should be armed if nothing was installed"


def test_enabling_refuses_when_the_watchdog_script_is_missing(sandbox, monkeypatch, tmp_path):
    """Registering a scheduler entry that points at nothing is worse than not
    registering one: it looks enabled and supervises nothing."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "watchdog_script", lambda: tmp_path / "missing.py")

    with pytest.raises(RuntimeError, match="Watchdog script missing"):
        au.enable()

    assert au.is_armed() is False
    assert sandbox.calls["launchctl"] == []


def test_enabling_on_macos_registers_the_agent_and_arms(sandbox, monkeypatch, watchdog_present):
    monkeypatch.setattr(au.sys, "platform", "darwin")

    au.enable()

    verbs = [c[0] for c in sandbox.calls["launchctl"]]
    assert "bootout" in verbs, "an existing job must be cleared first -- bootstrap refuses a loaded label"
    assert "bootstrap" in verbs
    assert au._PLIST_PATH.exists(), "the plist itself has to be written"
    assert au.is_armed() is True


def test_macos_falls_back_to_the_legacy_verb(sandbox, monkeypatch, watchdog_present):
    """Older macOS and some managed setups only have load/unload."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    seen = []

    def _lc(*args):
        seen.append(args)
        if args[0] == "bootstrap":
            return _completed(1, stderr="bootstrap unavailable")
        return _completed()

    monkeypatch.setattr(au, "_launchctl", _lc)
    au.enable()

    assert any(a[0] == "load" for a in seen), "it should have tried the legacy verb"
    assert au.is_armed() is True


def test_macos_raises_when_both_verbs_fail(sandbox, monkeypatch, watchdog_present):
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "_launchctl",
                        lambda *a: _completed(1, stderr="denied"))

    with pytest.raises(RuntimeError, match="launchctl bootstrap failed"):
        au.enable()

    assert au.is_armed() is False, "a failed install must not look armed"


def test_enabling_on_windows_creates_a_repeating_task(sandbox, monkeypatch, watchdog_present):
    monkeypatch.setattr(au.sys, "platform", "win32")
    monkeypatch.setattr(au, "_win_python", lambda: r"C:\App\.venv\Scripts\pythonw.exe")

    au.enable()

    args = sandbox.calls["schtasks"][0]
    assert args[0] == "/create"
    assert "/f" in args, "/f repairs an existing entry instead of failing on it"
    assert "/sc" in args and "minute" in args
    command = args[args.index("/tr") + 1]
    assert command.startswith('"') and command.count('"') == 4, (
        "interpreter and script must be quoted separately -- install paths "
        "contain spaces and would otherwise parse as two arguments"
    )
    assert au.is_armed() is True


def test_windows_raises_when_the_task_cannot_be_created(sandbox, monkeypatch, watchdog_present):
    monkeypatch.setattr(au.sys, "platform", "win32")
    monkeypatch.setattr(au, "_schtasks", lambda *a: _completed(1, stderr="access denied"))

    with pytest.raises(RuntimeError, match="schtasks /create failed"):
        au.enable()

    assert au.is_armed() is False


# ── disable() ─────────────────────────────────────────────────────────────────

def test_disabling_disarms_even_when_the_uninstall_fails(sandbox, monkeypatch):
    """Best-effort by design: the flag is what the watchdog reads, so it must
    come off whatever the scheduler says."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    au.arm()
    monkeypatch.setattr(au, "_mac_uninstall", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    au.disable()      # must not raise

    assert au.is_armed() is False


# ── sync_from_setting() ───────────────────────────────────────────────────────

def test_syncing_installs_when_the_setting_is_on_and_nothing_is_installed(sandbox, monkeypatch, watchdog_present):
    """Repairs an entry lost to an OS upgrade or a machine migration."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "is_installed", lambda: False)

    au.sync_from_setting(True)

    assert au.is_armed() is True
    assert [c[0] for c in sandbox.calls["launchctl"]], "it should have installed"


def test_syncing_only_re_arms_when_the_entry_is_already_there(sandbox, monkeypatch):
    """Re-arms after a stop script disarmed us, without reinstalling."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "is_installed", lambda: True)

    au.sync_from_setting(True)

    assert au.is_armed() is True
    assert sandbox.calls["launchctl"] == [], "no reinstall was needed"


def test_syncing_off_disarms(sandbox, monkeypatch):
    monkeypatch.setattr(au.sys, "platform", "darwin")
    au.arm()

    au.sync_from_setting(False)

    assert au.is_armed() is False


def test_syncing_never_raises_however_badly_it_fails(sandbox, monkeypatch):
    """It runs on app startup. A supervision feature that can block boot is
    worse than no supervision."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "is_installed",
                        lambda: (_ for _ in ()).throw(RuntimeError("scheduler gone")))

    au.sync_from_setting(True)      # must return quietly


def test_syncing_does_nothing_on_an_unsupported_platform(sandbox, monkeypatch):
    monkeypatch.setattr(au.sys, "platform", "linux")
    au.sync_from_setting(True)
    assert au.is_armed() is False


# ── is_installed() ────────────────────────────────────────────────────────────

def test_a_failing_install_check_reports_not_installed(sandbox, monkeypatch):
    """Guessing "installed" on an error would stop sync_from_setting repairing
    a broken entry."""
    monkeypatch.setattr(au.sys, "platform", "darwin")
    monkeypatch.setattr(au, "_mac_installed",
                        lambda: (_ for _ in ()).throw(RuntimeError("launchctl missing")))

    assert au.is_installed() is False
