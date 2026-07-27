"""Proves forex_trader.core.core_handle_conservative_trial's extracted
function behaves identically to SimulationEngine._handle_conservative_trial,
characterized in test_handle_conservative_trial_characterization.py -- see
docs/todo/refactor/core-conservative-trial-handler-migration/020-*.md.

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
from forex_trader.core import core_handle_conservative_trial as hct
from forex_trader.core import core_strategy_params as sp
from forex_trader.core.core_tp_trigger_tracking import TPCache
from backend.src.utils.models import STRATEGY_CONSERVATIVE_TRIAL


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
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", mt5_ticket=None,
                  lot_size=0.10, remaining_lots=0.10, stop_loss=2390.0,
                  entry_price=2400.0, sl_moved_to_be=0, **tps):
    with db.db() as conn:
        cols = ("trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
                "entry_price, lot_size, remaining_lots, stop_loss, sl_moved_to_be, "
                "status, open_time")
        vals = [trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price,
                lot_size, remaining_lots, stop_loss, sl_moved_to_be, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _insert_partial_close(trade_id, reason, lots_closed=0.005, ts=None):
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


def test_no_tp_cleared_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, tp1=2405.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2401.0, ask=2401.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_tp1_alone_closes_5pct_falls_through_to_tp2_check(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2405.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.005}]
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.095)
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_tp1_close_pct_is_live_tunable(fresh_db):
    sp.set_strategy_params(STRATEGY_CONSERVATIVE_TRIAL, {"tp1_pct": 10.0})
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2405.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))
    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]


def test_tp1_bridge_rejection_cascades_to_tp2_check_anyway(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, tp1=2405.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10
    assert bridge.modify_order_calls == []


def test_tp1_and_tp2_both_crossed_in_one_tick_cascades(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2390.0, tp1=2405.0, tp2=2410.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2411.0, ask=2411.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [
        {"ticket": 555, "lots": 0.005},
        {"ticket": 555, "lots": 0.03},
    ]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1


def test_tp2_alone_closes_30pct_moves_sl_to_be_unconditional_return(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.095,
                  entry_price=2400.0, stop_loss=2390.0, tp2=2410.0, tp3=2414.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2411.0, ask=2411.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0


def test_tp2_sl_already_moved_skips_move(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.095,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1, tp2=2410.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2411.0, ask=2411.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.03}]
    assert bridge.modify_order_calls == []


def test_tp3_alone_closes_20pct_falls_through_to_tp4_check(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.065,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1,
                  tp3=2414.0, tp4=2420.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2415.0, ask=2415.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.02}]
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.045)


def test_tp4_closes_40pct_steps_sl_to_tp2_level_unconditional_return(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.045,
                  entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1,
                  tp2=2410.0, tp4=2420.0, tp5=2427.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2")
    _insert_partial_close("t-1", "TP3")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2421.0, ask=2421.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2410.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2410.0


def test_tp4_sl_step_guard_skips_move_when_tp2_not_beyond_current_sl(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.045,
                  entry_price=2400.0, stop_loss=2415.0, sl_moved_to_be=1,
                  tp2=2410.0, tp4=2420.0)
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP2")
    _insert_partial_close("t-1", "TP3")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2421.0, ask=2421.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.04}]
    assert bridge.modify_order_calls == []


def test_tp5_alone_closes_5pct_falls_through_to_tp6_check(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.005,
                  entry_price=2400.0, stop_loss=2410.0, sl_moved_to_be=1,
                  tp5=2427.0, tp6=2435.0)
    for r in ("TP1", "TP2", "TP3", "TP4"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2428.0, ask=2428.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.005}]
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.0)


def test_tp6_closes_all_remaining_not_a_fixed_pct(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.005,
                  entry_price=2400.0, stop_loss=2410.0, sl_moved_to_be=1, tp6=2435.0)
    for r in ("TP1", "TP2", "TP3", "TP4", "TP5"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2436.0, ask=2436.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.005}]
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["remaining_lots"] == 0


def test_tp6_zero_remaining_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.0,
                  entry_price=2400.0, stop_loss=2410.0, sl_moved_to_be=1, tp6=2435.0)
    for r in ("TP1", "TP2", "TP3", "TP4", "TP5"):
        _insert_partial_close("t-1", r)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2436.0, ask=2436.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, tp1=2405.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hct.handle_conservative_trial(trade, _tick(bid=2406.0, ask=2406.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == pytest.approx(0.095)
