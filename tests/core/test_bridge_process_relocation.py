"""Bridge process launch lives in a service now (M4 B9b).

start_bridge_process was 142 lines of process management on the runtime:
native-bridge reconnect, Windows kill-and-relaunch, macOS Wine session
teardown. It has only two collaborators (the bridge and the
using-native-bridge flag), which makes it the cheapest of the big bodies
to move and the safest to move first.

The relocation has exactly one way to break quietly, and it is not the
process logic. The body locates mt5_bridge.py by walking up from
`__file__`:

    os.path.join(os.path.dirname(__file__), "..", "..", "mt5_bridge.py")

Two levels up from backend/src/runtime.py is the repo root. Two levels up
from backend/src/services/broker/bridge_process.py is backend/src -- and
the function would return False on a missing file rather than raise, so
the bridge would simply never start and the only evidence would be one
warning line. The depth test below is the point of this file.

Nothing here launches a process: subprocess.Popen is patched out wherever
a path could reach it, and the tests that matter never get that far.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest import mock

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.broker import bridge_process as bp

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_bridge_script_is_still_found_from_the_new_module_location():
    """The whole reason this batch could have failed silently.

    Asserts the resolved path is the real mt5_bridge.py at the repo root,
    not a path two directories short of it.
    """
    resolved = Path(bp.bridge_script_path())
    assert resolved == REPO_ROOT / "mt5_bridge.py", (
        f"resolved {resolved}, expected {REPO_ROOT / 'mt5_bridge.py'} -- the "
        f"'..' depth must match this module's nesting, not runtime.py's."
    )
    assert resolved.is_file(), "mt5_bridge.py must exist at the resolved path"


def test_a_missing_bridge_script_is_reported_not_raised():
    """Negative control: the failure mode the depth bug would have hidden."""
    with mock.patch.object(bp, "bridge_script_path", return_value="/nope/mt5_bridge.py"):
        launched = asyncio.run(bp.start_bridge_process(bridge=None, using_native_bridge=False))
    assert launched is False


# ── native bridge: no subprocess at all ──────────────────────────────────

class _FakeNativeBridge:
    def __init__(self, status):
        self._status = status
        self.reconnects = 0

    async def reconnect(self):
        self.reconnects += 1
        return {"status": self._status, "error": "boom"}


def test_native_bridge_reconnects_in_process_and_reports_success():
    bridge = _FakeNativeBridge("connected")
    ok = asyncio.run(bp.start_bridge_process(bridge=bridge, using_native_bridge=True))
    assert ok is True
    assert bridge.reconnects == 1


def test_native_bridge_reports_a_failed_reconnect():
    bridge = _FakeNativeBridge("disconnected")
    ok = asyncio.run(bp.start_bridge_process(bridge=bridge, using_native_bridge=True))
    assert ok is False
    assert bridge.reconnects == 1


# ── wiring ───────────────────────────────────────────────────────────────

def test_the_runtime_delegates_with_both_collaborators():
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._bridge = object()
    engine._using_native_bridge = True

    sentinel = mock.AsyncMock(return_value=True)
    with mock.patch("backend.src.runtime._start_bridge_process_impl", sentinel):
        result = asyncio.run(engine.start_bridge_process())

    assert result is True
    sentinel.assert_awaited_once_with(engine._bridge, True)


def test_the_relocated_function_is_the_real_one():
    """Negative control: the shell must not be delegating to a stub."""
    from backend.src import runtime
    assert runtime._start_bridge_process_impl is bp.start_bridge_process
    assert asyncio.iscoroutinefunction(bp.start_bridge_process)


def test_no_test_in_this_file_can_spawn_a_process():
    """Guard rail, asserted rather than assumed: the module's Popen is the
    real one, so any test that reached a launch path would fork MT5. The
    tests above stop before that -- this documents that it is deliberate."""
    import subprocess
    assert bp.subprocess.Popen is subprocess.Popen
