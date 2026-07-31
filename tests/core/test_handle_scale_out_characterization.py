"""Characterizes _handle_scale_out on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-scale-out-handler-migration/010-*.md.

Uses a fake bridge (partial_close/modify_order). NO real or demo MT5
order is ever placed, closed, or modified -- verified via the fake's own
call log.
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
        self._result = partial_close_result or {"success": True, "close_price": 2411.0, "lots_closed": None}
        self.partial_close_calls = []
        self.modify_order_calls = []

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        result = dict(self._result)
        if result.get("lots_closed") is None:
            result["lots_closed"] = lots
        return result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_trigger_cache = _TPCache()
    e._scale_out_last_fail = {}
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
                  lot_size=0.10, remaining_lots=0.10, sl_moved_to_be=0, stop_loss=2390.0,
                  entry_price=2400.0, **tps):
    with db.db() as conn:
        cols = ("trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
                "entry_price, lot_size, remaining_lots, stop_loss, sl_moved_to_be, status, open_time")
        vals = [trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price,
                lot_size, remaining_lots, stop_loss, sl_moved_to_be, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _insert_partial_close(trade_id, reason, lots_closed=0.04, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2410.0, 4.0, reason),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_no_tp_hit_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.partial_close_calls == []


def test_tp1_hit_closes_40pct_and_moves_sl_to_be(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]  # 40% of 0.10
    trade_after = _trade_dict("t-1")
    assert trade_after["sl_moved_to_be"] == 1
    assert trade_after["stop_loss"] == 2400.0  # entry price
    assert {"ticket": 555, "sl": 2400.0, "tp": None} in engine._bridge.modify_order_calls


def test_tp1_hit_again_does_not_move_sl_twice(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.06,
                  sl_moved_to_be=1, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    # only the SL-move modify_order call would show sl=2400 -- confirm none fired
    assert engine._bridge.modify_order_calls == []


def test_tp2_hit_closes_30pct_of_lot_size(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.06,
                  sl_moved_to_be=1, tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.04)  # TP1 already triggered
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2425.0, ask=2425.5)))
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]  # 30% of 0.10


def test_last_defined_tp_always_closes_full_remaining(fresh_db, engine):
    # TP4 is last_tp_num -- even though its tiered pct is only 10%, hitting it
    # closes 100% of whatever remains. TP1-3 marked already-triggered so only
    # TP4 is in this tick's hit set.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.01,
                  sl_moved_to_be=1, tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2")
    _insert_partial_close("t-1", "TP3")
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2445.0, ask=2445.5)))
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]  # all remaining


def test_bridge_rejection_records_cooldown_and_skips_retry(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})

    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert len(engine._bridge.partial_close_calls) == 1
    assert ("t-1", 1) in engine._scale_out_last_fail

    # Immediate retry within the cooldown window is skipped entirely.
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert len(engine._bridge.partial_close_calls) == 1  # no new call


def test_successful_retry_after_cooldown_clears_failure(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    engine._scale_out_last_fail[("t-1", 1)] = time.time() - 999  # long past cooldown

    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert len(engine._bridge.partial_close_calls) == 1
    assert ("t-1", 1) not in engine._scale_out_last_fail


def test_no_mt5_ticket_skips_bridge_still_records_partial(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scale_out(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.06  # 0.10 - 40%
