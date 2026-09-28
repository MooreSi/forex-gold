"""The VPS reports the commit it is RUNNING, not only the one checked out.

Found live 2026-09-28: the VPS had pulled 949a7228 but not restarted. Its
heartbeat's `commit` is read from disk on every beat, so the Mac's Remote tab
said "in sync" while the VPS ran the morning's code -- no trading status in
its heartbeat ("VPS Status Unknown ... update it if this persists", when it
was already updated), and no 5-minute placeholder write-off.

`running_commit` is the commit read once, on the first heartbeat after boot,
and the version check compares against it when the VPS sends it. A pulled but
unrestarted VPS is then "not in sync", and Upgrade VPS -- pull, then restart --
is the button that fixes it. A VPS that sends no `running_commit` (older code)
is compared on `commit` exactly as before.

No broker, no network: git is faked.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import _update_sync as us
from backend.src.services.risk import trading_status

BOOTED = "a" * 40
PULLED = "b" * 40


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    monkeypatch.setattr(us, "_running_commit", None)


class TestTheRunningCommit:
    def test_it_is_read_once_and_kept(self, monkeypatch):
        sha = [BOOTED]
        monkeypatch.setattr(us.core_app_update, "get_local_commit_sha",
                            lambda short=True: sha[0])

        assert us.running_commit() == BOOTED
        sha[0] = PULLED          # an update pulls; the process does not restart
        assert us.running_commit() == BOOTED

    def test_an_unreadable_first_read_is_retried(self, monkeypatch):
        """"" must not be cached: a git hiccup on the first beat would leave
        the running commit blank for the life of the process."""
        answers = iter([OSError("no git"), BOOTED])

        def _read(short=True):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        monkeypatch.setattr(us.core_app_update, "get_local_commit_sha", _read)

        assert us.running_commit() == ""
        assert us.running_commit() == BOOTED

    def test_the_vps_heartbeat_carries_it(self):
        import inspect
        from backend.src.services.cluster.sync import _telemetry
        assert "running_commit" in inspect.getsource(_telemetry.TelemetryMixin._status_payload)


class TestTheVersionCheck:
    def test_pulled_but_not_restarted_is_not_in_sync(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": PULLED, "git_version": ""})

        got = us.version_report({"commit": PULLED, "running_commit": BOOTED})

        assert got["in_sync"] is False
        assert got["remote"]["commit"] == BOOTED

    def test_it_says_a_restart_is_what_is_missing(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": PULLED, "git_version": ""})

        got = us.version_report({"commit": PULLED, "running_commit": BOOTED})

        assert got["restart_pending"] is True

    def test_restarted_is_in_sync(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": PULLED, "git_version": ""})

        got = us.version_report({"commit": PULLED, "running_commit": PULLED})

        assert got["in_sync"] is True
        assert got["restart_pending"] is False

    def test_an_older_vps_is_compared_as_before(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": PULLED, "git_version": ""})

        got = us.version_report({"commit": PULLED})

        assert got["in_sync"] is True
        assert got["restart_pending"] is None


class TestTheUnknownStatusSaysRestart:
    def test_the_message_does_not_only_say_update(self):
        view = {"connected": True, "badge": None}

        got = trading_status._as_the_vps_reports_it(view)

        assert got["state"] == "unknown"
        assert "restart" in got["detail"].lower()
