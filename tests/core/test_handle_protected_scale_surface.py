"""Proves backend.src.services.positions.handle_protected_scale's extracted
function behaves identically to SimulationEngine._handle_protected_scale,
characterized in test_handle_protected_scale_characterization.py -- see
docs/todo/refactor/core-protected-scale-handler-migration/020-*.md.

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

from backend.src.db import database as db
from backend.src.services.positions import handle_protected_scale as hps
from backend.src.services.risk import strategy_params as sp
from backend.src.services.positions.tp_tracking import TPCache
from backend.src.utils.models import STRATEGY_PROTECTED_SCALE
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
    sp._cache.clear()
    yield db
    sp._cache.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


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


def _insert_partial_close(trade_id, reason, lots_closed=0.0, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2410.0, 0.0, reason),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_tp1_cleared_marked_skipped_no_close_no_sl_move(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0, tp1=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    with db.db() as conn:
        reason = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=?", ("t-1",)
        ).fetchone()[0]
    assert reason == "TP1_SKIPPED"


def test_tp1_already_marked_not_reprocessed(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0, tp1=2410.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))
    with db.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM vantage_partial_closes WHERE trade_id=? AND reason=?",
            ("t-1", "TP1_SKIPPED"),
        ).fetchone()[0]
    assert count == 1


def test_tp2_cleared_moves_sl_to_be_no_partial_close(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0,
                  tp1=2410.0, tp2=2420.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_tp2_already_at_be_skips_modify_order(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))
    assert bridge.modify_order_calls == []


def test_tp3_cleared_closes_flat_20pct(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    _insert_partial_close("t-1", "TP2_BE_LOCKED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2435.0, ask=2435.5), bridge, TPCache()))
    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]


def test_mid_tp_close_pct_is_live_tunable(fresh_db):
    sp.set_strategy_params(STRATEGY_PROTECTED_SCALE, {"mid_tp_close_pct": 50.0})
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    _insert_partial_close("t-1", "TP2_BE_LOCKED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2435.0, ask=2435.5), bridge, TPCache()))
    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]


def test_break_on_first_miss_stops_before_later_tp(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    _insert_partial_close("t-1", "TP2_BE_LOCKED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_bridge_rejection_at_tp3_continues_to_tp4(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    _insert_partial_close("t-1", "TP2_BE_LOCKED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2445.0, ask=2445.5), bridge, TPCache()))
    assert len(bridge.partial_close_calls) == 2


def test_no_mt5_ticket_skips_bridge_still_records_partial(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2400.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1_SKIPPED")
    _insert_partial_close("t-1", "TP2_BE_LOCKED")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hps.handle_protected_scale(trade, _tick(bid=2435.0, ask=2435.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.08
