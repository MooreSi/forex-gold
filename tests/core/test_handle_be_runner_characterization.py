"""Characterizes _handle_be_runner on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-be-runner-handler-migration/010-*.md.

Uses a fake bridge (modify_order, plus partial_close for the ADX-ranging
fallback to _handle_scale_out). NO real or demo MT5 order is ever placed,
closed, or modified -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.src.db import database as db
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from backend.src.runtime import SimulationEngine


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
    def __init__(self):
        self.modify_order_calls = []
        self.partial_close_calls = []

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        return {"success": True, "close_price": 2411.0, "lots_closed": lots}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_trigger_cache = _TPCache()
    e._scale_out_last_fail = {}
    e._dpm_candles = None
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


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_adx_ranging_falls_back_to_scale_out(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    engine._dpm_candles = [{"h": 1, "l": 1, "c": 1}]
    with patch("backend.src.services.dpm.engine.compute_adx", return_value=15.0):
        asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    # scale_out's tiered 40% partial close fired instead of BE Runner's SL-only ratchet
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]


def test_adx_trending_runs_normal_be_runner_logic(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    engine._dpm_candles = [{"h": 1, "l": 1, "c": 1}]
    with patch("backend.src.services.dpm.engine.compute_adx", return_value=35.0):
        asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert engine._bridge.partial_close_calls == []  # BE Runner never partial-closes
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]


def test_no_tps_defined_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert engine._bridge.modify_order_calls == []


def test_price_below_tp1_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.modify_order_calls == []


def test_tp1_cleared_moves_sl_to_entry(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0,
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2412.0, ask=2412.5)))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_tp1_and_tp2_cleared_moves_sl_to_tp1(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2422.0, ask=2422.5)))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2410.0, "tp": None}]


def test_sl_already_at_target_rung_skips_update(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2400.0,  # already at BE
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2412.0, ask=2412.5)))
    assert engine._bridge.modify_order_calls == []


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, entry_price=2400.0, stop_loss=2390.0,
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_be_runner(engine, trade, _tick(bid=2412.0, ask=2412.5)))
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
