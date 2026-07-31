"""Characterizes _handle_orb_fixed on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-orb-fixed-handler-migration/010-*.md.

Uses a fake bridge (partial_close). NO real or demo MT5 order is ever
closed -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import database as db
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from forex_trader.core.engine import SimulationEngine


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


class _FakeBridge:
    def __init__(self, partial_close_result=None):
        self._result = partial_close_result or {"success": True, "close_price": 2411.0}
        self.partial_close_calls = []

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        return self._result


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_trigger_cache = _TPCache()
    return e


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", mt5_ticket=None,
                  remaining_lots=0.10, tp1=2410.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, tp1, "
            "status, open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, 2400.0, 0.10,
             remaining_lots, 2390.0, tp1, "open", time.time()),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_no_tp1_hit_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", tp1=2410.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_orb_fixed(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "open"


def test_no_remaining_lots_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", tp1=2410.0, remaining_lots=0.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_orb_fixed(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert engine._bridge.partial_close_calls == []


def test_tp1_hit_with_mt5_ticket_closes_full_remaining_via_bridge(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, remaining_lots=0.10)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": True, "close_price": 2411.5})
    asyncio.run(SimulationEngine._handle_orb_fixed(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.10}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["close_price"] == 2411.5  # bridge's actual close price used


def test_bridge_rejection_aborts_without_db_write(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, remaining_lots=0.10)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(SimulationEngine._handle_orb_fixed(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "open"
    assert trade_after["remaining_lots"] == 0.10


def test_no_mt5_ticket_skips_bridge_uses_signal_tp1_price(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, tp1=2410.0, remaining_lots=0.10)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_orb_fixed(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["close_price"] == 2410.0  # signal's own tp1, no bridge fill available
