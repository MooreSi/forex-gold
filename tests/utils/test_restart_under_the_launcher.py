"""An update or restart on Windows comes back up in the launcher's own window.

Reported 2026-09-26: the header's "update available" was pressed, the update
applied, and the app "never automatically loaded, just stopped". On Windows
`restart_app` spawned a hidden, detached relaunch and then exited 0.
"Setup & Start FOREX.bat" reads 0 as the user stopping the app, so its window
said "FOREX Trader has stopped", and the hidden copy -- with no window, no
crash protection and its output in a restart.log nobody reads -- was the only
thing meant to bring it back. (That time it hit a startup crash, invisibly.)

The launcher already relaunches on exit code 42; the admin console's update
used it all along. Now the launcher marks itself (FOREX_LAUNCHER=bat), and a
restart under it spawns nothing: it stops the server gracefully and asks for
42. Launched any other way, the detached relaunch is unchanged.

Nothing here starts a process: Popen is replaced by a recorder.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.src.utils import os_utils

REPO = Path(__file__).resolve().parents[2]
BAT = (REPO / "Setup & Start FOREX.bat").read_text(encoding="utf-8")


@pytest.fixture
def windows_launch(monkeypatch):
    spawned, stopped = [], []
    monkeypatch.setattr(os_utils.sys, "platform", "win32")
    monkeypatch.setattr(os_utils.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(os_utils, "shutdown_ui", lambda: stopped.append(1) or True)
    monkeypatch.setattr(os_utils, "_requested_exit_code", 0)
    return spawned, stopped


def test_under_the_launcher_it_asks_the_launcher_to_relaunch(windows_launch, monkeypatch, tmp_path):
    spawned, stopped = windows_launch
    monkeypatch.setenv("FOREX_LAUNCHER", "bat")

    os_utils.restart_app(tmp_path)

    assert spawned == [], "a second copy would race the launcher's own relaunch"
    assert stopped == [1], "the server is stopped gracefully, not killed"
    assert os_utils.requested_exit_code() == os_utils.LAUNCHER_RESTART_EXIT_CODE == 42


def test_the_launcher_marks_itself_before_starting_the_app():
    first_launch = BAT.index('"%VENV_PYTHON%" "%SCRIPT_DIR%run.py"')
    assert re.search(r'set "FOREX_LAUNCHER=bat"', BAT[:first_launch])


def test_the_launcher_relaunches_on_that_code():
    assert 'if "!_EXIT!"=="0" goto :stopped' in BAT
    assert "Exit code 42" in BAT


def test_run_py_exits_with_the_requested_code():
    run_py = (REPO / "run.py").read_text(encoding="utf-8")
    tail = run_py[run_py.index('if __name__ == "__main__":'):]
    assert "requested_exit_code()" in tail


def test_an_ordinary_run_still_exits_zero(monkeypatch):
    monkeypatch.setattr(os_utils, "_requested_exit_code", 0)
    assert os_utils.requested_exit_code() == 0
