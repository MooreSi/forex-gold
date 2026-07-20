"""Characterizes _handle_scalp_runner on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-scalp-runner-handler-migration/010-*.md.

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
        self._result = partial_close_result or {"success": True, "close_price": None, "lots_closed": None}
        self.partial_close_calls = []
        self.modify_order_calls = []

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        result = dict(self._result)
        if result.get("lots_closed") is None:
            result["lots_closed"] = lots
        if result.get("close_price") is None:
            result.pop("close_price", None)
        return result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_cache = {}
    e._tp_wait_log_ts = {}
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
                  lot_size=0.10, remaining_lots=0.10, stop_loss=2390.0,
                  entry_price=2400.0, **tps):
    with db.db() as conn:
        cols = ("trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
                "entry_price, lot_size, remaining_lots, stop_loss, status, open_time")
        vals = [trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price,
                lot_size, remaining_lots, stop_loss, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _insert_partial_close(trade_id, reason, lots_closed=0.05, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2403.0, 3.0, reason),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_no_tp1_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.partial_close_calls == []


def test_tp1_not_cleared_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2401.0, ask=2401.5)))
    assert engine._bridge.partial_close_calls == []


def test_tp1_cleared_closes_50pct_sl_untouched(fresh_db, engine):
    # _handle_scalp_runner's own phase-1 code never touches stop_loss (unlike
    # Conservative/BE-runner) -- it's meant to stay at its original -10pt
    # value until TP2. But partial_close_trade (pack 9) independently moves
    # SL to breakeven whenever reason=="TP1" and move_sl_to_be_after_tp1 is
    # on (the default), same characterization finding as pack 22
    # (core-conservative-handler-migration). So the DB row's SL still ends
    # up at breakeven here too, even though bridge.modify_order (the
    # broker-side sync) correctly never fires -- this handler has no
    # SL-move logic of its own in phase 1 to trigger it.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0  # set by partial_close_trade's own TP1 BE-move
    assert trade_after["sl_moved_to_be"] == 1
    assert trade_after["remaining_lots"] == 0.05


def test_tp1_bridge_rejection_no_db_write(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))

    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10


def test_tp1_auto_closed_returns_before_sl_move(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.01,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"


def test_tp1_done_tp2_not_cleared_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05,
                  entry_price=2400.0, stop_loss=2390.0, tp2=2404.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2403.5, ask=2404.0)))
    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == []


def test_tp2_cleared_moves_sl_to_be_no_partial_close(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05,
                  entry_price=2400.0, stop_loss=2390.0, tp2=2404.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))

    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_phase3_trails_remaining_with_fixed_distance(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05,
                  entry_price=2400.0, stop_loss=2400.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2", lots_closed=0.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2410.0, ask=2410.5)))

    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2407.0, "tp": None}]


def test_phase3_price_retreat_does_not_move_sl_backward(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05,
                  entry_price=2400.0, stop_loss=2407.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2", lots_closed=0.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.modify_order_calls == []


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_scalp_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))

    assert engine._bridge.partial_close_calls == []
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.05
