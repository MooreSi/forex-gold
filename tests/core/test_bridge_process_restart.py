"""Recovering the MT5 bridge when the watchdog decides it is wedged.

Three quite different recoveries live behind one function, and picking the
wrong one is worse than doing nothing:

  * **Native bridge** — there is no subprocess. Recovery means reconnecting the
    in-process MT5 session. Killing and relaunching here would be killing
    something that does not exist while the real session stays broken.
  * **Windows, HTTP bridge** — kill the bridge program, wait, force-kill, then
    relaunch with the port and credentials path it needs.
  * **macOS** — tear the whole Wine session down first, or the restart leaves
    duplicate MT5 windows behind.

The watchdog calls this when trading has already stopped working, so a
recovery that silently returns False and logs one line is the difference
between the app coming back and sitting dead until someone notices.

No processes are started or signalled: `kill_matching` and `Popen` are
recorded.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from backend.src.services.broker import bridge_process as bp
from backend.src.utils import os_utils

pytestmark = pytest.mark.asyncio


class _Bridge:
    def __init__(self, result=None):
        self._result = result if result is not None else {"status": "connected"}
        self.reconnects = 0

    async def reconnect(self):
        self.reconnects += 1
        return self._result


@pytest.fixture
def kills(monkeypatch):
    calls: list = []
    monkeypatch.setattr(os_utils, "kill_matching",
                        lambda pat, force=False: calls.append((pat, force)))
    monkeypatch.setattr(os_utils, "pids_matching", lambda pat: [])
    return calls


@pytest.fixture
def launches(monkeypatch):
    calls: list = []

    def _popen(argv, **kw):
        calls.append((argv, kw))
        return object()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    return calls


@pytest.fixture
def no_waiting(monkeypatch):
    """The real path sleeps 2s then 1s between kill attempts."""
    async def _sleep(_s):
        return None
    monkeypatch.setattr(bp.asyncio, "sleep", _sleep)


@pytest.fixture
def script_exists(monkeypatch, tmp_path):
    path = tmp_path / "mt5_bridge.py"
    path.write_text("# bridge", encoding="utf-8")
    monkeypatch.setattr(bp, "bridge_script_path", lambda: str(path))
    return path


class TestTheNativeBridgeIsReconnectedNotRelaunched:
    """There is no subprocess to kill. Killing here would signal nothing while
    the broken in-process session stayed broken."""

    async def test_it_reconnects(self, kills, launches):
        bridge = _Bridge()

        ok = await bp.start_bridge_process(bridge, using_native_bridge=True)

        assert ok is True
        assert bridge.reconnects == 1

    async def test_it_kills_and_launches_nothing(self, kills, launches):
        await bp.start_bridge_process(_Bridge(), using_native_bridge=True)

        assert kills == []
        assert launches == []

    async def test_a_failed_reconnect_reports_False(self, kills, launches):
        """The watchdog decides whether to keep trying based on this. Returning
        True on a failed reconnect makes it stop."""
        bridge = _Bridge({"status": "error", "error": "terminal not running"})

        ok = await bp.start_bridge_process(bridge, using_native_bridge=True)

        assert ok is False

    async def test_an_empty_result_is_not_read_as_success(self, kills,
                                                          launches):
        ok = await bp.start_bridge_process(_Bridge({}), using_native_bridge=True)

        assert ok is False


class TestAMissingScriptStopsBeforeAnythingIsKilled:
    async def test_it_returns_False(self, monkeypatch, kills, launches,
                                    tmp_path):
        monkeypatch.setattr(bp, "bridge_script_path",
                            lambda: str(tmp_path / "not_here.py"))

        ok = await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        assert ok is False
        assert kills == [], "killed the running bridge with nothing to restart"
        assert launches == []


@pytest.mark.skipif(sys.platform == "win32",
                    reason="patches sys.platform to win32; pointless on Windows")
class TestTheWindowsRestart:

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch, script_exists, no_waiting):
        monkeypatch.setattr(sys, "platform", "win32")

    async def test_it_kills_gently_then_forcefully(self, kills, launches):
        """A bridge holding an MT5 handle needs a chance to exit cleanly; a
        wedged one never will."""
        await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        assert kills == [("mt5_bridge.py", False), ("mt5_bridge.py", True)]

    async def test_it_launches_the_script_with_this_interpreter(self, kills,
                                                                launches):
        argv, _kw = launches[0] if launches else (None, None)

        await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        argv, kw = launches[-1]
        assert argv[0] == sys.executable
        assert argv[1].endswith("mt5_bridge.py")

    async def test_the_child_gets_its_port_and_credentials_path(self, kills,
                                                               launches):
        """Without these it starts on the wrong port, or with no credentials,
        and the app reports a bridge that will never connect."""
        await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        env = launches[-1][1]["env"]
        assert env["MT5_BRIDGE_PORT"]
        assert env["BRIDGE_CREDS_PATH"].endswith("bridge_credentials.json")

    async def test_it_detaches_so_the_bridge_outlives_this_process(
            self, kills, launches):
        await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        assert launches[-1][1]["start_new_session"] is True

    async def test_a_launch_failure_reports_False(self, kills, monkeypatch):
        def _boom(*_a, **_kw):
            raise OSError("no such interpreter")
        monkeypatch.setattr(subprocess, "Popen", _boom)

        ok = await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        assert ok is False

    async def test_a_successful_launch_reports_True(self, kills, launches):
        ok = await bp.start_bridge_process(_Bridge(), using_native_bridge=False)

        assert ok is True
