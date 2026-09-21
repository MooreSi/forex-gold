"""Starting the MT5 bridge at boot on macOS, where MT5 runs under Wine.

Nothing here reaches a broker. No MT5 order is placed, closed or modified:
`subprocess.Popen` is recorded and the port probe is answered by a stub.

The incident (2026-09-21, live, after a power cut). The Mac came back with
nothing running. The app started at 19:40:58 and logged "WINE_PYTHON not set
— skipping bridge auto-start", so it never launched the bridge at all. No
process was listening on 9010, so every tick request failed — 186 of them.
The bridge only came up at 19:45:09, when the bridge watchdog reached its
second consecutive failed health check and ran its OWN restart path, which
launches the same bridge under Wine perfectly well and has never needed
`WINE_PYTHON`. Ticks resumed at 19:45:20: four and a half minutes of a dead
app, all of it spent waiting for a watchdog to do what boot should have done.

`WINE_PYTHON` is set by nothing in this repo — not the launcher, not the
installer, not `Start MT5 Bridge.command`, which invokes Wine with
`C:\\Python311\\python.exe` as an ARGUMENT rather than as one interpreter
path. So that branch of `_start_mt5_bridge` has never once fired on a Mac;
the "auto-start" was dead code, and the watchdog masked it as a slow start.

The second test is the reason this is not simply "always launch it": a
restart while the bridge is healthy must not launch a second one. The Wine
relaunch tears down the whole Wine session, which would kill a working MT5
terminal underneath a running app.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

import run
from backend.src.services.broker import bridge_process as bp


@pytest.fixture
def launches(monkeypatch):
    calls: list = []

    def _popen(argv, **kw):
        calls.append((argv, kw))
        return object()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return calls


@pytest.fixture
def script_exists(monkeypatch, tmp_path):
    path = tmp_path / "mt5_bridge.py"
    path.write_text("# bridge", encoding="utf-8")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(bp, "bridge_script_path", lambda: str(path))
    return path


@pytest.fixture
def no_wine_python(monkeypatch):
    monkeypatch.delenv("WINE_PYTHON", raising=False)


@pytest.fixture
def nothing_on_the_port(monkeypatch):
    from backend.src.utils import os_utils
    monkeypatch.setattr(os_utils, "is_port_listening", lambda _p: False)


@pytest.fixture
def bridge_already_up(monkeypatch):
    from backend.src.utils import os_utils
    monkeypatch.setattr(os_utils, "is_port_listening", lambda _p: True)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the Wine launch path is macOS/Linux only")
class TestABootWithNoBridgeRunningStartsOne:

    def test_it_launches_the_bridge(self, script_exists, launches,
                                    no_wine_python, nothing_on_the_port):
        """The whole incident in one assertion: with no WINE_PYTHON in the
        environment — which is every Mac — this used to launch nothing."""
        run._start_mt5_bridge()

        assert launches, "boot started no bridge; the watchdog had to do it"

    def test_it_uses_the_same_command_the_watchdog_restart_uses(
            self, script_exists, launches, no_wine_python,
            nothing_on_the_port):
        """The watchdog's relaunch worked on the night boot did not. Two
        spellings of one launch is how they diverged in the first place."""
        run._start_mt5_bridge()

        argv, _kw = launches[-1]
        expected, _env = bp.wine_bridge_launch(str(script_exists))
        assert argv == expected

    def test_the_child_gets_its_port_and_credentials_path(
            self, script_exists, launches, no_wine_python,
            nothing_on_the_port):
        """Without BRIDGE_CREDS_PATH the bridge reads a sibling file that the
        app never writes, and connects with no credentials at all."""
        run._start_mt5_bridge()

        _argv, kw = launches[-1]

        env = kw["env"]
        assert env["MT5_BRIDGE_PORT"] == "9010"
        assert env["BRIDGE_CREDS_PATH"].endswith("bridge_credentials.json")
        assert env["WINEPREFIX"]

    def test_the_bridge_outlives_the_launcher(self, script_exists, launches,
                                              no_wine_python,
                                              nothing_on_the_port):
        run._start_mt5_bridge()

        _argv, kw = launches[-1]

        assert kw["start_new_session"] is True


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the Wine launch path is macOS/Linux only")
class TestARestartWithAHealthyBridgeLeavesItAlone:

    def test_it_launches_nothing(self, script_exists, launches,
                                 no_wine_python, bridge_already_up):
        """The Wine relaunch tears down wineserver and every child, MT5
        terminal included. Doing that to a healthy bridge because the app
        restarted would turn a 10-second restart into an outage."""
        run._start_mt5_bridge()

        assert launches == []
