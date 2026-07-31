"""Characterizes _run_tp_ladder and its three thin wrapper handlers
(_handle_signal_climber/_handle_reversal_runner/_handle_adaptive_runner) on
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
from backend.src.services.risk import strategy_params as sp
from forex_trader.core.core_tp_trigger_tracking import TPCache as _TPCache
from forex_trader.core.engine import SimulationEngine
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER


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


# ── Reversal Runner (be_at_pos=1) ─────────────────────────────────────────────────

def test_rr_tp1_hit_closes_15pct_does_not_move_sl_yet(fresh_db, engine):
    # n=3 -> _GDVR_PCTS[3] = [0.15, 0.25, 0.60]
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_reversal_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]
    assert engine._bridge.modify_order_calls == []  # SL untouched -- be_at_pos=1, this is pos=0


def test_rr_tp2_hit_after_tp1_moves_sl_to_be(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.085,
                  stop_loss=2380.0, tp1=2410.0, tp2=2420.0, tp3=2430.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.015)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_reversal_runner(engine, trade, _tick(bid=2425.0, ask=2425.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]  # now BE


def test_single_tick_clearing_multiple_tps_processes_both_in_one_call(fresh_db, engine):
    # A tick that clears TP1 AND TP2 in one shot (neither previously
    # triggered) processes both sequentially within the same call.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_reversal_runner(engine, trade, _tick(bid=2425.0, ask=2425.5)))

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


# ── Adaptive Runner 2 (be_at_pos=1, GDVR table, midpoint-lag2 trail) ──────────
# The behavior that's actually new here: every other ladder strategy trails
# SL to the single immediately-previous TP price after BE. Adaptive Runner 2
# trails to the MIDPOINT of the two TPs before the one just hit instead.

def test_adaptive_runner_2_tp1_hit_closes_10pct_does_not_move_sl_yet(fresh_db, engine):
    # n=5 -> _GDVR_PCTS[5] = [0.10, 0.10, 0.15, 0.25, 0.40]
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0, tp5=2450.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner_2(engine, trade, _tick(bid=2415.0, ask=2415.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]
    assert engine._bridge.modify_order_calls == []  # be_at_pos=1, this is pos=0


def test_adaptive_runner_2_tp2_hit_after_tp1_moves_sl_to_be(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.09, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0, tp5=2450.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.01)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner_2(engine, trade, _tick(bid=2425.0, ask=2425.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]  # BE


def test_adaptive_runner_2_tp3_hit_trails_to_midpoint_of_tp1_and_tp2(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  stop_loss=2400.0, sl_moved_to_be=1,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0, tp5=2450.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.01)
    _insert_partial_close("t-1", "TP2", lots_closed=0.01)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner_2(engine, trade, _tick(bid=2435.0, ask=2435.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.015}]
    # midpoint(tp1=2410, tp2=2420) = 2415 -- NOT tp2 (2420), which is what
    # every other ladder strategy would trail to at this position.
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2415.0, "tp": None}]


def test_adaptive_runner_2_tp4_hit_trails_to_midpoint_of_tp2_and_tp3(fresh_db, engine):
    """The exact scenario named in the strategy's own spec: at TP4, SL sits
    between TP2 and TP3."""
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.065,
                  stop_loss=2415.0, sl_moved_to_be=0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0, tp5=2450.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.01)
    _insert_partial_close("t-1", "TP2", lots_closed=0.01)
    _insert_partial_close("t-1", "TP3", lots_closed=0.015)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner_2(engine, trade, _tick(bid=2445.0, ask=2445.5)))

    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]
    # midpoint(tp2=2420, tp3=2430) = 2425
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2425.0, "tp": None}]


def test_adaptive_runner_2_never_loosens_sl_below_a_lower_midpoint(fresh_db, engine):
    """should_update's existing (direction-aware) guard must still hold --
    a degenerate/out-of-order TP set producing a midpoint below the current
    SL must not move it backward."""
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.08,
                  stop_loss=2426.0, sl_moved_to_be=1,   # already ahead of the midpoint below
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp4=2440.0, tp5=2450.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.01)
    _insert_partial_close("t-1", "TP2", lots_closed=0.01)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_adaptive_runner_2(engine, trade, _tick(bid=2435.0, ask=2435.5)))

    # midpoint(tp1, tp2) = 2415 < current_sl (2426) -- must not loosen.
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2426.0


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


# ── Limit Runner (dynamic per-signal pcts, tp_open leaves a runner leg) ────────
# The behavior that's actually new here: every other ladder strategy uses a
# fixed 8-TP-count table (_CLIMBER_PCTS/_GDVR_PCTS). Limit Runner splits
# evenly across however many numeric TPs THIS signal had, and — only when
# the signal carried a literal "TP OPEN" line (trade["tp_open"]) — reserves
# Strategy Parameters' runner_reserve_pct so the last TP doesn't close
# everything (close_full_on_last=False), leaving a permanently-open runner
# leg with no further TP to close it.

def test_limit_runner_tp_open_last_tp_only_closes_its_own_share(fresh_db, engine):
    # 3 TPs, tp_open=1 -> 75% split evenly = 25% each; last TP must NOT
    # close the remaining 25% reserve.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp_open=1)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]  # TP1: 25% of 0.10
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]  # BE at TP1 (default)


def test_limit_runner_tp_open_third_tp_leaves_reserve_open(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05, stop_loss=2400.0,
                  sl_moved_to_be=1,
                  tp1=2410.0, tp2=2420.0, tp3=2430.0, tp_open=1)
    _insert_partial_close("t-1", "TP1", lots_closed=0.025)
    _insert_partial_close("t-1", "TP2", lots_closed=0.025)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2435.0, ask=2435.5)))
    # closes only its own 25% share (0.025), NOT the full 0.05 remaining —
    # the other 0.025 keeps riding on the trailing SL with no further TP.
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.025}]
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.025


def test_limit_runner_without_tp_open_last_tp_closes_everything(fresh_db, engine):
    # No "TP OPEN" line -> tp_open=0 (default) -> 100% split evenly, last TP
    # closes all remaining like every other ladder strategy.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                  tp1=2410.0, tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    # Only TP1 reached this tick -- closes its own 50% share (n=2 -> 1/2 each).
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]


def test_limit_runner_without_tp_open_final_tp_closes_all_remaining(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.05, stop_loss=2400.0,
                  sl_moved_to_be=1, tp1=2410.0, tp2=2420.0)
    _insert_partial_close("t-1", "TP1", lots_closed=0.05)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2425.0, ask=2425.5)))
    assert engine._bridge.partial_close_calls == [{"ticket": 555, "lots": 0.05}]  # all remaining
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.0


def test_limit_runner_be_at_pos_strategy_param_is_1_based(fresh_db, engine):
    sp._cache.clear()
    sp.set_strategy_params(STRATEGY_LIMIT_RUNNER, {"be_at_pos": 2})  # BE at TP2, not TP1
    try:
        _insert_signal()
        _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0,
                      tp1=2410.0, tp2=2420.0, tp3=2430.0, tp_open=0)
        trade = _trade_dict("t-1")
        asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
        # TP1 hit but be_at_pos is now TP2 (compacted pos 1) -- no BE move yet.
        assert engine._bridge.modify_order_calls == []
    finally:
        sp.reset_strategy_params(STRATEGY_LIMIT_RUNNER)
        sp._cache.clear()


def test_limit_runner_no_tps_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10, stop_loss=2380.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_limit_runner(engine, trade, _tick(bid=2415.0, ask=2415.5)))
    assert engine._bridge.partial_close_calls == []
