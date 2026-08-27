"""Characterizes update_signal on SimulationEngine (core/engine.py) before
task 020 extracts it -- see docs/todo/refactor/core-update-signal-migration/010-*.md.

Uses a fake bridge (modify_order) and, for EA-managed scenarios,
ea_bridge.set_instance(fake). NO real or demo MT5 order is ever modified
-- verified via each fake's own call log.
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.runtime import TradingRuntime
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_SCALP_RUNNER, STRATEGY_BE_RUNNER,
)
from tests._fakes import _FakeBridge


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)
    ea_bridge.set_instance(None)


class _FakeEA:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.update_trade_calls = []

    def is_ea_healthy(self):
        return self._healthy

    async def update_trade(self, trade_id, tps, stop_loss=None):
        # stop_loss added 2026-08-04: while the EA is healthy, update_signal
        # deliberately skips its own modify_order, so this is now the ONLY
        # route a corrected stop has to the broker. Recorded here so the
        # tests below can assert it actually travels.
        self.update_trade_calls.append(
            {"trade_id": trade_id, "tps": tps, "stop_loss": stop_loss})
        return True


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    return e


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=None, managed_by="python", entry_price=2400.0,
                  stop_loss=2390.0, tp1=2410.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, tp1, "
            "status, open_time, strategy, managed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price, 0.10, 0.10,
             stop_loss, tp1, "open", time.time(), strategy, managed_by),
        )


def _get_trade(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def _get_signal(signal_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
        )


# ── basic field filtering ─────────────────────────────────────────────────────

def test_no_allowed_fields_returns_no_changes(fresh_db, engine):
    _insert_signal()
    result = asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"bogus_field": 1}))
    assert result == {"status": "no_changes"}


def test_disallowed_keys_dropped_allowed_keys_written(fresh_db, engine):
    _insert_signal()
    result = asyncio.run(TradingRuntime.update_signal(
        engine, "sig-1", {"stop_loss": 2385.0, "bogus_field": 1}))
    assert result["fields_updated"] == ["stop_loss"]
    sig = _get_signal("sig-1")
    assert sig["stop_loss"] == 2385.0


# ── no linked trade ────────────────────────────────────────────────────────────

def test_no_linked_open_trade(fresh_db, engine):
    _insert_signal()
    result = asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2385.0}))
    assert result["trade_updated"] is False
    assert result["trade_id"] is None


# ── no-propagate strategies ────────────────────────────────────────────────────

@pytest.mark.parametrize("strategy", [
    STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SCALP_RUNNER,
])
def test_no_propagate_strategies_leave_trade_untouched(fresh_db, engine, strategy):
    _insert_signal()
    _insert_trade("t-1", strategy=strategy, stop_loss=2395.0, tp1=2403.0)
    result = asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2380.0}))
    assert result["trade_updated"] is False
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2395.0  # untouched, still the fixed-point value


# ── propagation to a pure-sim trade (no mt5_ticket) ───────────────────────────

def test_propagates_to_sim_trade_without_bridge_call(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", strategy=STRATEGY_SCALE_OUT, mt5_ticket=None)
    result = asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2380.0}))
    assert result["trade_updated"] is True
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2380.0
    assert engine._bridge.modify_order_calls == []


# ── propagation to an MT5-backed trade ─────────────────────────────────────────

def test_new_sl_on_correct_side_sent_to_mt5(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2385.0}))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]


def test_new_sl_on_wrong_side_dropped_from_mt5_but_db_still_written(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    # BUY SL must be below entry(2400) -- 2405 is on the wrong side
    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2405.0}))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": None}]
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2405.0  # DB still has the raw value


def test_tp_sent_to_mt5_only_for_be_runner(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_BE_RUNNER,
                  mt5_ticket=555, entry_price=2400.0)
    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"tp1": 2420.0}))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": 2420.0}]


def test_tp_not_sent_to_mt5_for_non_be_runner(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"tp1": 2420.0}))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": None}]


# ── EA-managed trades ──────────────────────────────────────────────────────────

def test_ea_managed_healthy_skips_modify_order_calls_update_trade(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="ea")
    fake_ea = _FakeEA(healthy=True)
    ea_bridge.set_instance(fake_ea)

    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2385.0, "tp1": 2420.0}))

    assert engine._bridge.modify_order_calls == []
    assert fake_ea.update_trade_calls == [
        {"trade_id": "t-1", "tps": {1: 2420.0}, "stop_loss": 2385.0}
    ]


def test_ea_managed_unhealthy_falls_through_to_modify_order_but_still_updates_ea(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="ea")
    fake_ea = _FakeEA(healthy=False)
    ea_bridge.set_instance(fake_ea)

    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2385.0, "tp1": 2420.0}))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]
    assert fake_ea.update_trade_calls == [
        {"trade_id": "t-1", "tps": {1: 2420.0}, "stop_loss": 2385.0}
    ]


def test_python_managed_never_calls_ea_update_trade(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="python")
    fake_ea = _FakeEA(healthy=True)
    ea_bridge.set_instance(fake_ea)  # configured, but trade isn't EA-managed

    asyncio.run(TradingRuntime.update_signal(engine, "sig-1", {"stop_loss": 2385.0}))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]
    assert fake_ea.update_trade_calls == []
