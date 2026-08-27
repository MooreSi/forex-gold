"""Proves backend.src.services.positions.handle_conservative's extracted function
behaves identically to SimulationEngine._handle_conservative,
characterized in test_handle_conservative_characterization.py -- see
docs/todo/refactor/core-conservative-handler-migration/020-*.md.

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
from backend.src.services.positions import handle_conservative as hc
from backend.src.services.positions.tp_tracking import TPCache
from tests._fakes import _FakeBridge


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
                  lot_size=0.10, remaining_lots=0.10, stop_loss=2395.0,
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


def _insert_partial_close(trade_id, reason, lots_closed=0.08, ts=None):
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


def test_no_tp1_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_tp1_not_cleared_is_noop(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2395.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2401.0, ask=2401.5), bridge, TPCache()))
    assert bridge.partial_close_calls == []


def test_tp1_cleared_closes_80pct_and_moves_sl_to_be(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2395.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.08}]
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1
    assert trade_after["remaining_lots"] == 0.02


def test_tp1_bridge_rejection_no_db_write(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2395.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge(partial_close_result={"success": False, "error": "rejected"})
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))

    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10
    assert trade_after["sl_moved_to_be"] == 0


def test_tp1_auto_closed_skips_modify_order_but_db_sl_still_moves(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.01,
                  entry_price=2400.0, stop_loss=2395.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))

    assert bridge.partial_close_calls == [{"ticket": 555, "lots": 0.01}]
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["status"] == "closed"
    assert trade_after["stop_loss"] == 2400.0


def test_phase2_trails_remaining_20pct_with_fixed_distance(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.02,
                  entry_price=2400.0, stop_loss=2400.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2410.0, ask=2410.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2407.0, "tp": None}]


def test_phase2_price_retreat_does_not_move_sl_backward(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, lot_size=0.10, remaining_lots=0.02,
                  entry_price=2400.0, stop_loss=2407.0)
    _insert_partial_close("t-1", "TP1")
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))
    assert bridge.modify_order_calls == []


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, lot_size=0.10, remaining_lots=0.10,
                  entry_price=2400.0, stop_loss=2395.0, tp1=2403.0)
    trade = _trade_dict("t-1")
    bridge = _FakeBridge()
    asyncio.run(hc.handle_conservative(trade, _tick(bid=2405.0, ask=2405.5), bridge, TPCache()))

    assert bridge.partial_close_calls == []
    assert bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.02
    assert trade_after["stop_loss"] == 2400.0
