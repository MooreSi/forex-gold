"""Characterizes _run_tp_ladder and its three thin wrapper handlers
(_handle_signal_climber/_handle_gd_vip_runner/_handle_adaptive_runner) on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-tp-ladder-handlers-migration/010-*.md.

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


# ── no-op ──────────────────────────────────────────────────────────────────────

def test_no_tp_hit_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.partial_close_calls == []


# ── Signal Climber (be_at_pos=0) ────────────────────────────────────────────────

def test_climber_tp1_hit_closes_30pct_and_moves_sl_to_be(fresh_db, engine):
    # n=3 -> _CLIMBER_PCTS[3] = [0.30, 0.30, 0.40]
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_climber_tp3_last_closes_full_remaining_returns_before_sl_trail(fresh_db, engine):
    # The last TP fully empties the position (auto_closed=True) -- when the
    # trade is MT5-backed, the function schedules _close_full_after_tps
    # (fire-and-forget) and returns IMMEDIATELY, before ever reaching the
    # SL-trail block below. There's nothing left to protect, so no
    # modify_order call happens here, unlike a mid-ladder TP.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.04,
                  stop_loss=2400.0, sl_moved_to_be=1,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.03)
    _insert_partial_close("t-1", "TP2", lots_closed=0.03)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2435.0, ask=2435.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]  # all remaining
    assert engine._bridge.modify_order_calls == []  # early return -- no SL trail attempted


# ── GD VIP Runner (be_at_pos=1) ─────────────────────────────────────────────────

def test_gdvr_tp1_hit_closes_15pct_does_not_move_sl_yet(fresh_db, engine):
    # n=3 -> _GDVR_PCTS[3] = [0.15, 0.25, 0.60]
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_gd_vip_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]
    assert engine._bridge.modify_order_calls == []  # SL untouched -- be_at_pos=1, this is pos=0


def test_gdvr_tp2_hit_after_tp1_moves_sl_to_be(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.085,
                  stop_loss=2380.0, tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.015)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_gd_vip_runner(engine, trade, _tick(bid=2425.0, ask=2425.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]  # now BE


def test_single_tick_clearing_multiple_tps_processes_both_in_one_call(fresh_db, engine):
    # A tick that clears TP1 AND TP2 in one shot (neither previously
    # triggered) processes both sequentially within the same call.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_gd_vip_runner(engine, trade, _tick(bid=2425.0, ask=2425.5)))

    assert engine._bridge.partial_close_calls == [
        {"ticket": 555, "lots": 0.015},  # TP1: 15%
        {"ticket": 555, "lots": 0.025},  # TP2: 25% of original 0.10, clamped to remaining
    ]
    # be_at_pos=1 reached on TP2 within this same call -> SL moves to BE
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]


# ── Adaptive Runner (be_at_pos=0, GDVR table) ──────────────────────────────────

def test_adaptive_runner_tp1_hit_closes_15pct_moves_sl_to_be_immediately(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]  # GDVR table
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]  # BE at TP1


# ── shared engine behaviors ──────────────────────────────────────────────────────

def test_wrong_side_tp_excluded_from_ladder(fresh_db, engine):
    _insert_signal()
    # tp1 below entry -- not a valid BUY target, excluded entirely
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, tp1=2395.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2425.0, ask=2425.5)))
    # ladder has only 1 real TP (tp2) -> n=1 -> _CLIMBER_PCTS[1] = [1.00] -> full close
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.10}]


def test_gap_in_tp_sequence_does_not_truncate_ladder(fresh_db, engine):
    _insert_signal()
    # tp2 is NULL, tp3 populated -- must still be reachable
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, tp1=2410.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2435.0, ask=2435.5)))
    # n=2 (tp1, tp3) -> _CLIMBER_PCTS[2] = [0.40, 0.60] -- both processed in one tick
    assert len(engine._bridge.partial_close_calls) == 2


def test_bridge_rejection_at_one_tp_continues_loop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    engine._bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2425.0, ask=2425.5)))
    # Both TP1 and TP2 attempted (rejection doesn't abort the walk), neither succeeded
    assert len(engine._bridge.partial_close_calls) == 2
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10  # nothing actually closed


def test_no_mt5_ticket_skips_bridge_still_records_partial(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_signal_climber(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.07  # 0.10 - 30%
