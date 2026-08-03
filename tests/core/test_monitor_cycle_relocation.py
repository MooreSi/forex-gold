"""One monitor cycle lives in a service now (M4 B9d).

_monitor_loop was 159 lines: a `while self._monitor_running` shell wrapped
around a cycle that reads a tick, dispatches every open trade to its
strategy handler, runs the pending-signal watcher and the IME watchdog,
then ticks four counters that fire MT5 reconciliation, the profit sweep and
DPM calibration on their own cadences.

The shell stays on the runtime -- it owns the task's lifetime. The cycle
moves to services/positions/monitor_cycle.py.

This is the batch where a value-style context would have been wrong, and
that is what most of this file tests. The cycle mutates state that has to
survive INTO THE NEXT CYCLE:

  - four counters (sync/profit/cal/dxy) whose whole purpose is to count
    cycles. Reset them each pass and MT5 reconciliation, the profit sweep
    and DPM calibration never fire again.
  - has_open_trades / has_pending_signals, which decide the 1s-vs-5s
    adaptive sleep AND deliberately keep their previous value when a tick
    comes back empty.

So the ctx carries a MonitorState object by reference. A copy would look
correct in review, pass a naive wiring test, and quietly disable three
background jobs in production.

No order is placed here: the strategy handlers, the close context and the
reconciliation call are all sentinels.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.runtime import SimulationEngine
from backend.src.services.positions import monitor_cycle as mc


def _engine():
    e = SimulationEngine.__new__(SimulationEngine)
    e._monitor_running = True
    e._bridge = object()
    e._cfg = {"starting_balance": 1000.0}
    e._dpm_candles = []
    e._tp_trigger_cache = object()
    e._dpm_cache = object()
    e._scale_out_last_fail = {}
    e._pending_activation_retry_after = {}
    e._monitor_state = mc.MonitorState()
    return e


# ── the state object ─────────────────────────────────────────────────────

def test_the_counters_survive_between_cycles():
    """The failure a copied context would cause, asserted directly."""
    engine = _engine()
    first = engine._make_monitor_ctx()
    first.state.sync_cycle = 5
    second = engine._make_monitor_ctx()
    assert second.state is first.state
    assert second.state.sync_cycle == 5, (
        "the monitor state must be shared by reference across cycles -- a "
        "copy resets the counters and MT5 sync/profit sweep/DPM calibration "
        "never fire."
    )


def test_the_adaptive_poll_flags_are_part_of_that_state():
    engine = _engine()
    ctx = engine._make_monitor_ctx()
    for flag in ("has_open_trades", "has_pending_signals"):
        assert hasattr(ctx.state, flag), flag


def test_the_state_starts_where_the_old_attributes_started():
    """The counters used to be __init__ attributes seeded to 0."""
    state = mc.MonitorState()
    assert state.sync_cycle == 0
    assert state.profit_cycle == 0
    assert state.cal_cycle == 0
    assert state.dxy_cycle == 0
    assert state.has_open_trades is False
    assert state.has_pending_signals is False
    assert state.dpm_dxy_candles == []


# ── the context ──────────────────────────────────────────────────────────

EXPECTED_CTX_FIELDS = [
    "state",
    "bridge",
    "cfg",
    "tp_trigger_cache",
    "dpm_cache",
    "scale_out_last_fail",
    "pending_activation_retry_after",
    "get_dpm_candles",
    "set_dpm_candles",
    "get_tick",
    "get_open_trades",
    "get_candles",
    "is_trading_paused",
    "background_open_commentary",
    "close_full_after_tps",
    "make_close_trade_ctx",
    "sync_closed_mt5_positions",
]


@pytest.mark.parametrize("field", EXPECTED_CTX_FIELDS)
def test_the_context_carries_every_collaborator(field):
    ctx = _engine()._make_monitor_ctx()
    assert hasattr(ctx, field), f"MonitorCtx is missing {field}"


def test_the_bound_collaborators_point_back_at_the_engine():
    engine = _engine()
    ctx = engine._make_monitor_ctx()
    assert ctx.bridge is engine._bridge
    assert ctx.cfg is engine._cfg
    assert ctx.tp_trigger_cache is engine._tp_trigger_cache
    assert ctx.dpm_cache is engine._dpm_cache
    assert ctx.scale_out_last_fail is engine._scale_out_last_fail
    assert ctx.pending_activation_retry_after is engine._pending_activation_retry_after


def test_dpm_candles_still_reads_and_writes_the_engine_attribute():
    """_dpm_candles is read outside the loop too -- by open_trade_from_signal
    and by the scan context -- so the cycle must go on writing the engine's
    own attribute, not a private copy inside the state."""
    engine = _engine()
    ctx = engine._make_monitor_ctx()

    ctx.set_dpm_candles([{"o": 1}])
    assert engine._dpm_candles == [{"o": 1}]
    assert ctx.get_dpm_candles() == [{"o": 1}]

    engine._dpm_candles = [{"o": 2}]
    assert ctx.get_dpm_candles() == [{"o": 2}], "reads must be live, not snapshotted"


# ── the shell ────────────────────────────────────────────────────────────

def test_the_loop_shell_runs_cycles_until_told_to_stop():
    """The shell owns the task lifetime; the service owns one pass."""
    engine = _engine()
    calls = []

    async def fake_cycle(ctx):
        calls.append(ctx)
        if len(calls) >= 3:
            engine._monitor_running = False
        return False

    with mock.patch("backend.src.runtime._run_monitor_cycle_impl", fake_cycle), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        asyncio.run(engine._monitor_loop())

    assert len(calls) == 3
    assert all(isinstance(c, mc.MonitorCtx) for c in calls)


def test_the_shell_polls_fast_when_the_cycle_says_so():
    """1s with trades open or a signal queued, 5s when nothing is pending --
    the adaptive cadence is the shell's decision, driven by the cycle."""
    engine = _engine()
    slept = []

    async def fake_sleep(secs):
        slept.append(secs)
        engine._monitor_running = False

    for fast, expected in [(True, 1), (False, 5)]:
        engine._monitor_running = True
        slept.clear()

        async def fake_cycle(ctx, _fast=fast):
            return _fast

        with mock.patch("backend.src.runtime._run_monitor_cycle_impl", fake_cycle), \
             mock.patch("asyncio.sleep", new=fake_sleep):
            asyncio.run(engine._monitor_loop())

        assert slept == [expected], f"fast_poll={fast} should sleep {expected}s"


def test_the_relocated_function_is_the_real_one():
    from backend.src import runtime
    assert runtime._run_monitor_cycle_impl is mc.run_monitor_cycle
    assert asyncio.iscoroutinefunction(mc.run_monitor_cycle)


def test_the_close_path_is_untouched_by_this_batch():
    assert hasattr(SimulationEngine, "_make_close_trade_ctx")
    assert hasattr(SimulationEngine, "close_trade")
    assert hasattr(SimulationEngine, "record_close")
