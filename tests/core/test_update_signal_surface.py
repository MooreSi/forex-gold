"""Proves backend.src.services.trading.update_signal's extracted function
behaves identically to SimulationEngine.update_signal, characterized in
test_update_signal_characterization.py -- see
docs/todo/refactor/core-update-signal-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever modified -- verified via each
fake's own call log.
"""
import asyncio
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.trading import update_signal as us
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_SCALP_RUNNER, STRATEGY_BE_RUNNER,
)


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


class _FakeBridge:
    def __init__(self):
        self.modify_order_calls = []

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


class _FakeEA:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.update_trade_calls = []

    def is_ea_healthy(self):
        return self._healthy

    async def update_trade(self, trade_id, tps):
        self.update_trade_calls.append({"trade_id": trade_id, "tps": tps})
        return True


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

def test_no_allowed_fields_returns_no_changes(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(us.update_signal(bridge, "sig-1", {"bogus_field": 1}))
    assert result == {"status": "no_changes"}


def test_disallowed_keys_dropped_allowed_keys_written(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0, "bogus_field": 1}))
    assert result["fields_updated"] == ["stop_loss"]
    sig = _get_signal("sig-1")
    assert sig["stop_loss"] == 2385.0


# ── no linked trade ────────────────────────────────────────────────────────────

def test_no_linked_open_trade(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0}))
    assert result["trade_updated"] is False
    assert result["trade_id"] is None


# ── no-propagate strategies ────────────────────────────────────────────────────

@pytest.mark.parametrize("strategy", [
    STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SCALP_RUNNER,
])
def test_no_propagate_strategies_leave_trade_untouched(fresh_db, strategy):
    _insert_signal()
    _insert_trade("t-1", strategy=strategy, stop_loss=2395.0, tp1=2403.0)
    bridge = _FakeBridge()
    result = asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2380.0}))
    assert result["trade_updated"] is False
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2395.0


# ── propagation to a pure-sim trade (no mt5_ticket) ───────────────────────────

def test_propagates_to_sim_trade_without_bridge_call(fresh_db):
    _insert_signal()
    _insert_trade("t-1", strategy=STRATEGY_SCALE_OUT, mt5_ticket=None)
    bridge = _FakeBridge()
    result = asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2380.0}))
    assert result["trade_updated"] is True
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2380.0
    assert bridge.modify_order_calls == []


# ── propagation to an MT5-backed trade ─────────────────────────────────────────

def test_new_sl_on_correct_side_sent_to_mt5(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    bridge = _FakeBridge()
    asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0}))
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]


def test_new_sl_on_wrong_side_dropped_from_mt5_but_db_still_written(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    bridge = _FakeBridge()
    asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2405.0}))
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": None}]
    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2405.0


def test_tp_sent_to_mt5_only_for_be_runner(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_BE_RUNNER,
                  mt5_ticket=555, entry_price=2400.0)
    bridge = _FakeBridge()
    asyncio.run(us.update_signal(bridge, "sig-1", {"tp1": 2420.0}))
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": 2420.0}]


def test_tp_not_sent_to_mt5_for_non_be_runner(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0)
    bridge = _FakeBridge()
    asyncio.run(us.update_signal(bridge, "sig-1", {"tp1": 2420.0}))
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": None, "tp": None}]


# ── EA-managed trades ──────────────────────────────────────────────────────────

def test_ea_managed_healthy_skips_modify_order_calls_update_trade(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="ea")
    fake_ea = _FakeEA(healthy=True)
    ea_bridge.set_instance(fake_ea)
    bridge = _FakeBridge()

    asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0, "tp1": 2420.0}))

    assert bridge.modify_order_calls == []
    assert fake_ea.update_trade_calls == [{"trade_id": "t-1", "tps": {1: 2420.0}}]


def test_ea_managed_unhealthy_falls_through_to_modify_order_but_still_updates_ea(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="ea")
    fake_ea = _FakeEA(healthy=False)
    ea_bridge.set_instance(fake_ea)
    bridge = _FakeBridge()

    asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0, "tp1": 2420.0}))

    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]
    assert fake_ea.update_trade_calls == [{"trade_id": "t-1", "tps": {1: 2420.0}}]


def test_python_managed_never_calls_ea_update_trade(fresh_db):
    _insert_signal()
    _insert_trade("t-1", direction="BUY", strategy=STRATEGY_SCALE_OUT,
                  mt5_ticket=555, entry_price=2400.0, managed_by="python")
    fake_ea = _FakeEA(healthy=True)
    ea_bridge.set_instance(fake_ea)
    bridge = _FakeBridge()

    asyncio.run(us.update_signal(bridge, "sig-1", {"stop_loss": 2385.0}))

    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2385.0, "tp": None}]
    assert fake_ea.update_trade_calls == []
