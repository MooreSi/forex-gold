"""Finding processes must not confuse "none" with "the tool broke".

`pids_matching` backs the bridge watchdog: it finds the Wine/MT5 processes so
they can be killed and restarted. On Windows it shells out to `wmic`, and it
wrapped the whole thing in `except Exception: return []` -- so a missing or
failing `wmic` looks exactly like "no such process is running". `kill_matching`
then reports 0 killed and the watchdog concludes there was nothing to restart.

That matters more than it used to: Windows clients are in scope (owner,
2026-09-02), and `wmic` is deprecated -- Microsoft has been removing it from
recent Windows builds. If it is gone on a client's machine, the recovery path
goes quiet rather than failing.

`test_the_current_process_is_discoverable` is the empirical half. It runs on
every platform, and on Windows CI it answers the question this repo cannot
answer from a Mac: does the mechanism actually work there?
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

from backend.src.utils import os_utils


class TestAFailureIsNotSilent:
    def test_a_broken_lookup_is_logged(self, monkeypatch, caplog):
        """Returning [] quietly is what makes a dead watchdog look healthy."""
        def _boom(*a, **kw):
            raise FileNotFoundError("wmic")

        monkeypatch.setattr(os_utils.subprocess, "check_output", _boom)

        with caplog.at_level("WARNING"):
            result = os_utils.pids_matching("anything")

        assert result == []
        assert any("anything" in r.getMessage() or "look up" in r.getMessage().lower()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    def test_no_matches_is_not_logged_as_a_failure(self, monkeypatch, caplog):
        """The ordinary case must stay quiet, or the log fills with noise and
        the real failure is buried in it."""
        monkeypatch.setattr(
            os_utils.subprocess, "check_output",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, "pgrep")))

        with caplog.at_level("WARNING"):
            os_utils.pids_matching("definitely-not-running-xyzzy")

        assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


class TestItActuallyFindsProcesses:
    def test_a_process_we_started_is_found_by_its_command_line(self):
        """The empirical check, and the reason this file exists.

        A child is spawned carrying a marker unique to this run, so the
        assertion cannot pass by accident and cannot depend on what else is
        running. Read-only: it never calls kill_matching, and it stops its own
        child directly.

        An earlier version searched for the CURRENT process using
        `basename(sys.executable)` and failed — this venv's real command line
        is `.../Python.app/Contents/MacOS/Python`, capital P, and `pgrep -f`
        is case-sensitive. The lookup was right; the needle was wrong.

        On Windows CI this is the only thing in the repo that proves the
        `wmic` path still works, which matters because Microsoft has been
        removing `wmic` from recent builds.
        """
        marker = f"forex-pidprobe-{uuid.uuid4().hex[:12]}"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             f"import time; _ = {marker!r}; time.sleep(30)"])
        try:
            deadline = time.time() + 10
            pids = []
            while time.time() < deadline:
                pids = os_utils.pids_matching(marker)
                if pids:
                    break
                time.sleep(0.2)

            assert pids, (
                f"no process matched {marker!r}, yet one was just started with "
                "it on its command line — the process-lookup mechanism is "
                "broken on this platform, and the bridge watchdog silently "
                "believes nothing needs restarting")
            # That process, specifically. "returned a non-empty list" is
            # satisfied by a lookup that fabricates PIDs, and the watchdog
            # kills whatever this returns.
            assert proc.pid in pids, (
                f"matched {pids} for {marker!r}, which does not include the "
                f"process that carries it ({proc.pid})")
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_a_nonsense_pattern_finds_nothing(self):
        """Negative control: the assertion above would be satisfied by a
        lookup that returned every PID on the machine."""
        assert os_utils.pids_matching("zzz-no-such-process-zzz-4f2a") == []
