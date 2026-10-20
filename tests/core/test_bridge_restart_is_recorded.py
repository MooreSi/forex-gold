"""Every bridge restart is recorded on the runtime, whoever asked for it.

The watchdog, the self-healer and the bot's /restartbridge all restart the
bridge through `TradingRuntime.start_bridge_process`. The self-healer reads
the time it records to give a just-restarted MT5 its cold-start grace
(2026-10-19: the two restarters took turns, 46 restarts in 2.5 hours).

NOTHING IS RESTARTED: the real teardown/launch is replaced by a recorder.
"""
from __future__ import annotations

import time

import pytest

from backend.src import runtime as rt
from backend.src.runtime import TradingRuntime


@pytest.mark.asyncio
async def test_a_restart_records_when_it_happened(monkeypatch):
    launched = []

    async def _fake_impl(bridge, using_native_bridge):
        launched.append(bridge)
        return True

    monkeypatch.setattr(rt, "_start_bridge_process_impl", _fake_impl)
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._bridge = object()
    engine._using_native_bridge = False

    before = time.monotonic()
    assert await engine.start_bridge_process() is True

    assert launched == [engine._bridge]
    assert before <= engine._bridge_restarted_at <= time.monotonic()
