"""Proves backend.src.services.positions.tp_ladder's extracted functions
behave identically to SimulationEngine._run_tp_ladder and its three
wrapper handlers, characterized in
test_run_tp_ladder_characterization.py -- see
docs/todo/refactor/core-tp-ladder-handlers-migration/020-*.md.

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
from backend.src.services.positions import tp_ladder as ladder
from backend.src.services.risk import strategy_params as sp
from backend.src.services.positions.tp_tracking import TPCache
from backend.src.utils.models import STRATEGY_SIGNAL_CLIMBER
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


def _insert_partial_close(trade_id, reason, lots_closed=0.03, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2410.0, 3.0, reason),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_no_tp_hit_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_climber_tp1_hit_closes_30pct_and_moves_sl_to_be(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_climber_be_at_pos_is_live_tunable(fresh_db):
    # Default be_at_pos=1 (TP1) moves SL to BE on TP1 -- raising it to 2
    # (TP2) must withhold that move until TP2, same as Reversal Runner's
    # hardcoded be_at_pos=1 (0-indexed) behavior.
    sp.set_strategy_params(STRATEGY_SIGNAL_CLIMBER, {"be_at_pos": 2.0})
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]
    assert bridge.modify_order_calls == []


def test_climber_tp3_last_closes_full_remaining_returns_before_sl_trail(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.04,
                  stop_loss=2400.0, sl_moved_to_be=1,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.03)
    _insert_partial_close("t-1", "TP2", lots_closed=0.03)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2435.0, ask=2435.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]
    assert bridge.modify_order_calls == []


def test_rr_tp1_hit_closes_15pct_does_not_move_sl_yet(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_reversal_runner(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]
    assert bridge.modify_order_calls == []


def test_rr_tp2_hit_after_tp1_moves_sl_to_be(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.085,
                  stop_loss=2380.0, tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.015)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_reversal_runner(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]


def test_single_tick_clearing_multiple_tps_processes_both_in_one_call(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_reversal_runner(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [
        {"ticket": 555, "lots": 0.015},
        {"ticket": 555, "lots": 0.025},
    ]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]


def test_adaptive_runner_tp1_hit_closes_15pct_moves_sl_to_be_immediately(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_adaptive_runner(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]


def test_wrong_side_tp_excluded_from_ladder(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, tp1=2395.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))
    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.10}]


def test_gap_in_tp_sequence_does_not_truncate_ladder(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, tp1=2410.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2435.0, ask=2435.5), bridge, TPCache()))
    assert len(bridge.partial_close_calls) == 2


def test_bridge_rejection_at_one_tp_continues_loop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2425.0, ask=2425.5), bridge, TPCache()))
    assert len(bridge.partial_close_calls) == 2
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10


def test_no_mt5_ticket_skips_bridge_still_records_partial(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(ladder.handle_signal_climber(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.07
