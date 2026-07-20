"""Proves forex_trader.core.core_handle_no_sl_scale's extracted function
behaves identically to SimulationEngine._handle_no_sl_scale, characterized
in test_handle_no_sl_scale_characterization.py -- see
docs/todo/refactor/core-no-sl-scale-handler-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
verified via the fake bridge's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_handle_no_sl_scale as hnss
from forex_trader.core.core_tp_trigger_tracking import TPCache


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


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2380.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", mt5_ticket=None,
                  lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
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


def _insert_partial_close(trade_id, reason, lots_closed=0.0, ts=None):
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


def _partial_close_reasons(trade_id):
    with db.db() as conn:
        rows = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=? ORDER BY ts", (trade_id,)
        ).fetchall()
        return [r[0] for r in rows]


def test_no_tps_defined_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_tp1_cleared_last_tp_closes_all(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0, tp1=2405.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.10}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["remaining_lots"] == 0


def test_tp1_cleared_more_tps_exist_20pct_close_falls_through(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.08)
    assert trade_after["stop_loss"] == 2400.0


def test_tp1_behind_entry_market_at_be_partial_closes_at_market_price(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0,
                  tp1=2395.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2400.0, ask=2400.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.08)


def test_tp1_behind_entry_market_not_at_be_marks_skipped(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0,
                  tp1=2395.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2398.0, ask=2398.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    assert _partial_close_reasons("t-1") == ["TP1_SKIPPED"]


def test_tp2_cleared_last_tp_closes_all(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  entry_price=2400.0, stop_loss=2400.0, tp1=2405.0, tp2=2410.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.02)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2411.0, ask=2411.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.08}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"


def test_tp2_cleared_more_tps_exist_marks_skipped_only(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.02)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2411.0, ask=2411.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    assert _partial_close_reasons("t-1") == ["TP1", "TP2_SKIPPED"]
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.08


def test_tp3_cleared_last_tp_closes_all(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.02)
    _insert_partial_close("t-1", "TP2_SKIPPED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2416.0, ask=2416.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.08}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"


def test_tp3_cleared_more_tps_exist_20pct_close_moves_sl_to_tp1(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.02)
    _insert_partial_close("t-1", "TP2_SKIPPED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2416.0, ask=2416.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2405.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2405.0
    assert trade_after["remaining_lots"] == pytest.approx(0.06)


def test_tp1_tp2_tp3_cascade_sl_move_uses_stale_current_sl(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0, tp5=2425.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2416.0, ask=2416.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [
        {"ticket": 555, "lots": 0.02},
        {"ticket": 555, "lots": 0.02},
    ]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2405.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2405.0
    assert _partial_close_reasons("t-1") == ["TP1", "TP2_SKIPPED", "TP3"]


def test_tp4_not_last_steps_sl_to_tp2_level_marks_skipped(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.06,
                  entry_price=2400.0, stop_loss=2405.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0, tp5=2425.0)
    for r in ("TP1", "TP2_SKIPPED", "TP3"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2421.0, ask=2421.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2410.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2410.0
    assert _partial_close_reasons("t-1") == ["TP1", "TP2_SKIPPED", "TP3", "TP4_SKIPPED"]


def test_tp4_sl_step_guard_skips_move_when_not_beyond_current_sl(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.06,
                  entry_price=2400.0, stop_loss=2412.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0, tp5=2425.0)
    for r in ("TP1", "TP2_SKIPPED", "TP3"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2421.0, ask=2421.5), bridge, TPCache()))

    assert bridge.modify_order_calls == []
    assert _partial_close_reasons("t-1") == ["TP1", "TP2_SKIPPED", "TP3", "TP4_SKIPPED"]


def test_last_defined_tp_within_loop_range_closes_all(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.04,
                  entry_price=2400.0, stop_loss=2410.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0, tp5=2425.0)
    for r in ("TP1", "TP2_SKIPPED", "TP3", "TP4_SKIPPED"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2426.0, ask=2426.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["remaining_lots"] == 0


def test_tp8_final_branch_closes_all_when_max_tp_defined(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.02,
                  entry_price=2400.0, stop_loss=2420.0,
                  tp1=2405.0, tp2=2410.0, tp3=2415.0, tp4=2420.0,
                  tp5=2425.0, tp6=2430.0, tp7=2435.0, tp8=2440.0)
    for r in ("TP1", "TP2_SKIPPED", "TP3", "TP4_SKIPPED", "TP5_SKIPPED", "TP6_SKIPPED", "TP7_SKIPPED"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2441.0, ask=2441.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2380.0,
                  tp1=2405.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hnss.handle_no_sl_scale(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.08)
