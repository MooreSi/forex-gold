"""Finding processes must not confuse "none" with "the tool broke".

`pids_matching` backs the bridge watchdog: it finds the Wine/MT5 processes so
they can be killed and restarted. On Windows it shells out to `wmic`, and it
wrapped the whole thing in `except Exception: return []` -- so a missing or
failing `wmic` looks exactly like "no such process is running". `kill_matching`
then reports 0 killed and the watchdog concludes there was nothing to restart.

That matters more than it used to: Windows clients are in scope (owner,
2026-09-02), and `wmic` is deprecated -- Microsoft has been removing it from
recent Windows builds.

**It is not hypothetical.** On 2026-09-02 the Windows CI run failed this
file's empirical test: a process started with a unique marker on its command
line was not found. `wmic` is now a fallback; PowerShell's CIM query is the
primary path, because it is present on every supported Windows.

`tasklist` cannot do this job at all -- it reports image names, not command
lines, and the watchdog matches on "wineserver", "mt5_bridge.py" and
"terminal64.exe", two of which are not image names.

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


class TestTheWindowsLookup:
    """Simulated, so the mechanism is pinned from a Mac. The real behaviour is
    proved on Windows CI by TestItActuallyFindsProcesses below."""

    @pytest.fixture
    def on_windows(self, monkeypatch):
        monkeypatch.setattr(os_utils.sys, "platform", "win32")

    def test_powershell_is_tried_before_wmic(self, on_windows, monkeypatch):
        """wmic is deprecated and missing from recent builds; CIM is not."""
        calls: list = []
        monkeypatch.setattr(
            os_utils, "_pids_windows_powershell",
            lambda p: calls.append("powershell") or [42])
        monkeypatch.setattr(
            os_utils, "_pids_windows_wmic",
            lambda p: calls.append("wmic") or [99])

        assert os_utils.pids_matching("x") == [42]
        assert calls == ["powershell"]

    def test_wmic_is_the_fallback_when_powershell_cannot_answer(
            self, on_windows, monkeypatch):
        monkeypatch.setattr(os_utils, "_pids_windows_powershell", lambda p: None)
        monkeypatch.setattr(os_utils, "_pids_windows_wmic", lambda p: [99])

        assert os_utils.pids_matching("x") == [99]

    def test_an_empty_result_is_NOT_treated_as_a_failure(self, on_windows,
                                                          monkeypatch):
        """"Nothing matched" is a real answer. Falling through to wmic on an
        empty list would reintroduce the confusion this change removes."""
        wmic: list = []
        monkeypatch.setattr(os_utils, "_pids_windows_powershell", lambda p: [])
        monkeypatch.setattr(os_utils, "_pids_windows_wmic",
                            lambda p: wmic.append("called") or [])

        assert os_utils.pids_matching("x") == []
        assert wmic == [], "fell back to wmic on a legitimate empty result"

    def test_both_mechanisms_failing_is_logged_at_ERROR(self, on_windows,
                                                         monkeypatch, caplog):
        """The watchdog is blind at this point. It must say so loudly."""
        monkeypatch.setattr(os_utils, "_pids_windows_powershell", lambda p: None)
        monkeypatch.setattr(os_utils, "_pids_windows_wmic", lambda p: None)

        with caplog.at_level("ERROR"):
            assert os_utils.pids_matching("x") == []

        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_a_quote_in_the_pattern_is_refused(self, monkeypatch):
        """The pattern is interpolated into a single-quoted PowerShell string.
        Every real caller passes a literal, so a quote means something has gone
        wrong -- refuse rather than build a command that means something else."""
        ran: list = []
        monkeypatch.setattr(os_utils.subprocess, "run",
                            lambda *a, **kw: ran.append(a))

        assert os_utils._pids_windows_powershell("bad'pattern") is None
        assert ran == []

    def test_a_powershell_error_is_not_reported_as_no_matches(self,
                                                              monkeypatch):
        """The whole bug class, in the new mechanism. A non-zero exit must
        return None so the wmic fallback runs -- returning [] would say
        "nothing is running" about a lookup that never ran."""
        class _R:
            returncode = 1
            stdout = ""
            stderr = "Get-CimInstance : Access denied"

        monkeypatch.setattr(os_utils.subprocess, "run", lambda *a, **kw: _R())

        assert os_utils._pids_windows_powershell("x") is None

    def test_a_powershell_crash_is_not_reported_as_no_matches(self,
                                                              monkeypatch):
        """A machine with no powershell.exe at all."""
        def _boom(*a, **kw):
            raise FileNotFoundError("powershell")

        monkeypatch.setattr(os_utils.subprocess, "run", _boom)

        assert os_utils._pids_windows_powershell("x") is None

    def test_the_cim_query_matches_on_the_command_line(self, monkeypatch):
        """Not the image name. Two of the three real patterns
        ("mt5_bridge.py", "wineserver") never appear as an image name."""
        seen: list = []

        class _R:
            returncode = 0
            stdout = "123\n"
            stderr = ""

        monkeypatch.setattr(os_utils.subprocess, "run",
                            lambda cmd, **kw: seen.append(cmd) or _R())

        assert os_utils._pids_windows_powershell("mt5_bridge.py") == [123]
        script = seen[0][-1]
        assert "CommandLine" in script
        assert "mt5_bridge.py" in script

    def test_the_query_excludes_itself(self, monkeypatch):
        """The pattern goes into the command line of the query being run, so
        without this the lookup finds its own PowerShell process and every
        call returns one spurious pid -- including calls that should match
        nothing. Found on Windows CI by the nonsense-pattern control."""
        seen: list = []

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(os_utils.subprocess, "run",
                            lambda cmd, **kw: seen.append(cmd) or _R())

        os_utils._pids_windows_powershell("wineserver")

        assert "$_.ProcessId -ne $PID" in seen[0][-1]


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

            if not pids:
                # The suite runs at -q on CI, so captured warnings do not reach
                # the log. When this failed on 2026-09-02 the message said only
                # "no process matched", and which mechanism broke could not be
                # recovered from the run at all. Put the diagnosis IN the
                # assertion, where it survives.
                detail = ""
                if sys.platform == "win32":
                    ps = os_utils._pids_windows_powershell(marker)
                    wm = os_utils._pids_windows_wmic(marker)
                    detail = (f" powershell returned {ps!r}, wmic returned "
                              f"{wm!r} (None means the tool could not answer)")
                raise AssertionError(
                    f"no process matched {marker!r}, yet one was just started "
                    f"with it on its command line (pid {proc.pid}).{detail} "
                    "The bridge watchdog cannot see processes on this "
                    "platform and will not restart a dead bridge.")
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
