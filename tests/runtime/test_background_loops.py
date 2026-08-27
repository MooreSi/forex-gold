"""The runtime's background supervisor loops.

`backend/src/runtime.py` is the last of the three areas the 2026-08-25 merge
pushed below its coverage floor, and most of what is uncovered are these loops:
signal scanning, the closed-market queue flush, REF backfill, the TP safety
net, data retention, snapshot capture, the email scheduler, signal-bus pruning.

They all share one shape and one contract:

    while self._monitor_running:
        try:    <work>
        except asyncio.CancelledError:  break
        except Exception:               log and carry on
        await asyncio.sleep(interval)

The contract is what matters. Each of these is the *only* thing doing its job,
so a single bad cycle taking the loop down means that job silently stops for
the lifetime of the process -- no error, no retry, just a queue that never
flushes again or a safety net that never sweeps. Every test here is some form
of "one bad cycle costs one cycle".

Nothing reaches a broker: the runtime is built with __new__ so __init__ never
runs, no bridge is ever constructed, and every unit of work is replaced.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src import runtime as rt
from backend.src.runtime import TradingRuntime


class _RunsFor:
    """A `_monitor_running` flag that goes False after N reads.

    `while self._monitor_running:` calls __bool__ each pass, so this bounds the
    loop at N cycles without needing to cancel it.
    """

    def __init__(self, cycles: int):
        self.cycles = cycles
        self.reads = 0

    def __bool__(self) -> bool:
        self.reads += 1
        return self.reads <= self.cycles


@pytest.fixture
def instant_sleep(monkeypatch):
    """Collapse every await asyncio.sleep(...) in the runtime, including the
    startup delays, so a loop's cycles run immediately."""
    async def _sleep(_seconds, *a, **k):
        return None
    monkeypatch.setattr(rt.asyncio, "sleep", _sleep)


def _engine(cycles=2, **attrs):
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._monitor_running = _RunsFor(cycles)
    engine._bridge = object()
    engine._cfg = {}
    engine._tg_reader = None
    for k, v in attrs.items():
        setattr(engine, k, v)
    return engine


def _counter():
    """An async unit of work that records each call."""
    calls = []

    async def _work(*a, **k):
        calls.append(True)
    return calls, _work


def _thrower(calls):
    async def _work(*a, **k):
        calls.append(True)
        raise RuntimeError("cycle exploded")
    return _work


# ── _ref_backfill_loop ────────────────────────────────────────────────────────

def test_the_backfill_loop_runs_once_per_cycle(instant_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(rt.db_module, "to_db_thread",
                        lambda fn, *a, **k: _done(calls))
    engine = _engine(cycles=3, _REF_BACKFILL_INTERVAL=0)

    asyncio.run(engine._ref_backfill_loop())

    assert len(calls) == 3


def _done(calls):
    async def _f():
        calls.append(True)
    return _f()


def test_a_failing_backfill_costs_one_cycle_not_the_loop(instant_sleep, monkeypatch):
    calls = []

    def _to_db(fn, *a, **k):
        async def _f():
            calls.append(True)
            raise RuntimeError("backfill exploded")
        return _f()

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    engine = _engine(cycles=3, _REF_BACKFILL_INTERVAL=0)

    asyncio.run(engine._ref_backfill_loop())      # must not raise

    assert len(calls) == 3, "every cycle should still have been attempted"


def test_cancelling_the_backfill_loop_stops_it_cleanly(instant_sleep, monkeypatch):
    def _to_db(fn, *a, **k):
        async def _f():
            raise asyncio.CancelledError()
        return _f()

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    engine = _engine(cycles=99, _REF_BACKFILL_INTERVAL=0)

    asyncio.run(engine._ref_backfill_loop())

    assert engine._monitor_running.reads < 5, "cancellation must break, not spin"


# ── _tp_safety_net_loop ───────────────────────────────────────────────────────

def test_the_safety_net_sweeps_every_cycle(instant_sleep):
    calls, work = _counter()
    engine = _engine(cycles=2, _TP_SAFETY_NET_INTERVAL=0, _tp_safety_net_sweep=work)

    asyncio.run(engine._tp_safety_net_loop())

    assert len(calls) == 2


def test_a_failing_sweep_does_not_retire_the_safety_net(instant_sleep):
    """It is the backstop for TP levels the ladder missed. Losing it silently
    is exactly the failure it exists to catch."""
    calls = []
    engine = _engine(cycles=3, _TP_SAFETY_NET_INTERVAL=0,
                     _tp_safety_net_sweep=_thrower(calls))

    asyncio.run(engine._tp_safety_net_loop())

    assert len(calls) == 3


# ── _closed_market_queue_loop ─────────────────────────────────────────────────

def test_the_queue_is_only_flushed_when_the_setting_is_on(instant_sleep, monkeypatch):
    flushed = []

    def _to_db(fn, *a, **k):
        async def _f():
            return {"lk_queue_closed_market_limits": 0}
        return _f()

    async def _flush(rs, place_fn):
        flushed.append(True)

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    monkeypatch.setattr(rt, "flush_queued_limits", _flush)
    engine = _engine(cycles=2, _CLOSED_MARKET_FLUSH_INTERVAL=0)

    asyncio.run(engine._closed_market_queue_loop())

    assert flushed == []


def test_the_queue_is_flushed_when_the_setting_is_on(instant_sleep, monkeypatch):
    flushed = []

    def _to_db(fn, *a, **k):
        async def _f():
            return {"lk_queue_closed_market_limits": 1}
        return _f()

    async def _flush(rs, place_fn):
        flushed.append(rs)

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    monkeypatch.setattr(rt, "flush_queued_limits", _flush)
    engine = _engine(cycles=2, _CLOSED_MARKET_FLUSH_INTERVAL=0)

    asyncio.run(engine._closed_market_queue_loop())

    assert len(flushed) == 2


def test_a_failing_flush_does_not_stop_the_queue_loop(instant_sleep, monkeypatch):
    """Weekend limit setups sit in this queue. A loop that died on one bad
    flush would drop every one of them until the next restart."""
    attempts = []

    def _to_db(fn, *a, **k):
        async def _f():
            return {"lk_queue_closed_market_limits": 1}
        return _f()

    async def _flush(rs, place_fn):
        attempts.append(True)
        raise RuntimeError("flush exploded")

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    monkeypatch.setattr(rt, "flush_queued_limits", _flush)
    engine = _engine(cycles=3, _CLOSED_MARKET_FLUSH_INTERVAL=0)

    asyncio.run(engine._closed_market_queue_loop())

    assert len(attempts) == 3


# ── _signal_snapshot_loop ─────────────────────────────────────────────────────

def test_snapshots_are_captured_every_cycle(instant_sleep, monkeypatch):
    calls, work = _counter()
    monkeypatch.setattr(rt, "_capture_signal_snapshots_impl", work)
    engine = _engine(cycles=2, _SIGNAL_SNAPSHOT_INTERVAL=0)

    asyncio.run(engine._signal_snapshot_loop())

    assert len(calls) >= 2


def test_a_failing_snapshot_capture_never_stops_the_loop(instant_sleep, monkeypatch):
    """core_signal_snapshot's own docstring: a research log must never be able
    to stop a trade. The loop around it has to hold that line too."""
    calls = []
    monkeypatch.setattr(rt, "_capture_signal_snapshots_impl", _thrower(calls))
    engine = _engine(cycles=3, _SIGNAL_SNAPSHOT_INTERVAL=0)

    asyncio.run(engine._signal_snapshot_loop())

    assert len(calls) == 3


# ── _signal_scanner_loop ──────────────────────────────────────────────────────

def test_the_scanner_runs_each_cycle(instant_sleep):
    calls, work = _counter()
    engine = _engine(cycles=2, _scan_messages=work)

    asyncio.run(engine._signal_scanner_loop())

    assert len(calls) == 2


def test_a_failing_scan_does_not_stop_signal_processing(instant_sleep):
    """This loop is how Telegram messages become signals at all. If one bad
    message could end it, the app would go quiet with no error visible."""
    calls = []
    engine = _engine(cycles=3, _scan_messages=_thrower(calls))

    asyncio.run(engine._signal_scanner_loop())

    assert len(calls) == 3


# ── _email_scheduler_loop ─────────────────────────────────────────────────────

def test_a_failing_email_sweep_does_not_stop_the_scheduler(instant_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(rt, "_email_scheduler_sweep_impl", _thrower(calls))
    engine = _engine(cycles=3, _is_active_trader_node=lambda: True)

    asyncio.run(engine._email_scheduler_loop())

    assert len(calls) == 3


# ── _signal_bus_prune_loop ────────────────────────────────────────────────────

def test_the_bus_is_pruned_every_cycle(instant_sleep, monkeypatch):
    calls = []
    monkeypatch.setattr(rt.db_module, "prune_signal_bus", lambda: calls.append(True))
    engine = _engine(cycles=2)

    asyncio.run(engine._signal_bus_prune_loop())

    assert len(calls) == 2


def test_a_failing_prune_does_not_stop_the_loop(instant_sleep, monkeypatch):
    """prune_signal_bus existed but was never called from anywhere, so the
    table grew unbounded. Having wired it up, it needs to stay wired."""
    calls = []

    def _boom():
        calls.append(True)
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(rt.db_module, "prune_signal_bus", _boom)
    engine = _engine(cycles=3)

    asyncio.run(engine._signal_bus_prune_loop())

    assert len(calls) == 3


# ── _data_retention_loop ──────────────────────────────────────────────────────

def test_a_failing_retention_sweep_does_not_stop_the_loop(instant_sleep, monkeypatch):
    calls = []

    def _to_db(fn, *a, **k):
        async def _f():
            calls.append(True)
            raise RuntimeError("prune exploded")
        return _f()

    monkeypatch.setattr(rt.db_module, "to_db_thread", _to_db)
    engine = _engine(cycles=3, _DATA_RETENTION_INTERVAL=0)

    asyncio.run(engine._data_retention_loop())

    assert len(calls) == 3
