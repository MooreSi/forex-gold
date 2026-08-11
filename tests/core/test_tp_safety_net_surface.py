"""Proves backend.src.services.positions.safety_net's extracted functions
behave identically to SimulationEngine's originals, characterized in
test_tp_safety_net_characterization.py -- see
docs/todo/refactor/core-tp-safety-net-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.positions import safety_net as tpsn


class _FakeBridge:
    def __init__(self, candles=None, modify_result=None, tick=None):
        self._candles = candles if candles is not None else []
        self._modify_result = modify_result or {"success": True}
        self._tick = tick or SimpleNamespace(bid=2400.0, ask=2400.5)
        self.modify_order_calls = []

    async def get_candles_range(self, start, end, tf):
        return self._candles

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return self._modify_result

    async def get_tick(self):
        return self._tick


def _trade(strategy="scale_out", sl_moved_to_be=0, managed_by="python", tp1=2410.0,
          tp2=None, open_time=None, entry_price=2400.0, direction="BUY",
          mt5_ticket=555, trade_id="t-1"):
    return {
        "trade_id": trade_id, "strategy": strategy, "sl_moved_to_be": sl_moved_to_be,
        "managed_by": managed_by, "tp1": tp1, "tp2": tp2,
        "open_time": open_time if open_time is not None else time.time() - 100,
        "entry_price": entry_price, "direction": direction, "mt5_ticket": mt5_ticket,
    }


def _insert_trade_row(trade_id="t-1", mt5_ticket=555, stop_loss=2380.0, direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, 2400.0, 2400.0, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, 2400.0, 2400.0, 2400.0,
             0.10, 0.10, stop_loss, "open", time.time()),
        )


def _row(trade_id="t-1"):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute(
                "SELECT stop_loss, sl_moved_to_be FROM vantage_simulated_trades WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
        )


def test_be_runner_strategy_is_noop(fresh_db):
    bridge = _FakeBridge()
    t = _trade(strategy="be_runner")
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_already_protected_is_noop(fresh_db):
    bridge = _FakeBridge()
    t = _trade(sl_moved_to_be=1)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_ea_managed_healthy_ea_is_noop(fresh_db):
    bridge = _FakeBridge()
    t = _trade(managed_by="ea")
    fake_ea = SimpleNamespace(is_ea_healthy=lambda: True)
    with mock.patch.object(ea_bridge, "get_instance", return_value=fake_ea):
        asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_ea_managed_no_instance_proceeds(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    bridge = _FakeBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2405.0, ask=2405.5))
    t = _trade(managed_by="ea")
    with mock.patch.object(ea_bridge, "get_instance", return_value=None):
        asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls != []


def test_reversal_runner_uses_tp2_not_tp1(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    bridge = _FakeBridge(candles=[{"high": 2422.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2405.0, ask=2405.5))
    t = _trade(strategy="reversal_runner", tp1=2410.0, tp2=2420.0)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls != []


def test_no_trigger_tp_value_is_noop(fresh_db):
    bridge = _FakeBridge()
    t = _trade(tp1=None)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_too_young_is_noop(fresh_db):
    bridge = _FakeBridge()
    t = _trade(open_time=time.time() - 5)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_no_candles_is_noop(fresh_db):
    bridge = _FakeBridge(candles=[])
    t = _trade()
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_extreme_not_reached_is_noop(fresh_db):
    bridge = _FakeBridge(candles=[{"high": 2405.0, "low": 2398.0}])
    t = _trade(tp1=2410.0)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []


def test_reached_protects_and_writes_db(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    bridge = _FakeBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2405.0, ask=2405.5))
    t = _trade(tp1=2410.0)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.35, "tp": None}]
    assert _row() == {"stop_loss": 2400.35, "sl_moved_to_be": 1}


def test_window_closed_no_move_no_write(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    bridge = _FakeBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2400.0, ask=2400.5))
    t = _trade(tp1=2410.0)
    with mock.patch.object(tpsn, "compute_be_cost_pts", return_value=1.0):
        asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []
    assert _row() == {"stop_loss": 2380.0, "sl_moved_to_be": 0}


def test_modify_order_raises_no_write(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    class _RaisingBridge(_FakeBridge):
        async def modify_order(self, ticket, sl=None, tp=None):
            raise RuntimeError("mt5 down")
    bridge = _RaisingBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                            tick=SimpleNamespace(bid=2405.0, ask=2405.5))
    t = _trade(tp1=2410.0)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert _row() == {"stop_loss": 2380.0, "sl_moved_to_be": 0}


def test_modify_order_rejected_no_write(fresh_db):
    _insert_trade_row(stop_loss=2380.0)
    bridge = _FakeBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2405.0, ask=2405.5),
                         modify_result={"success": False, "error": "invalid stops"})
    t = _trade(tp1=2410.0)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert _row() == {"stop_loss": 2380.0, "sl_moved_to_be": 0}


def test_no_mt5_ticket_skips_broker_still_writes_db(fresh_db):
    _insert_trade_row(stop_loss=2380.0, mt5_ticket=None)
    bridge = _FakeBridge(candles=[{"high": 2412.0, "low": 2398.0}],
                         tick=SimpleNamespace(bid=2405.0, ask=2405.5))
    t = _trade(tp1=2410.0, mt5_ticket=None)
    asyncio.run(tpsn.tp_safety_net_check_trade(t, time.time(), bridge, {}))
    assert bridge.modify_order_calls == []
    assert _row() == {"stop_loss": 2400.35, "sl_moved_to_be": 1}


def test_sweep_continues_past_per_trade_exception(fresh_db):
    calls = []
    async def fake_check(trade, now, bridge, last_alert):
        calls.append(trade["trade_id"])
        if trade["trade_id"] == "bad":
            raise RuntimeError("boom")
    with mock.patch.object(tpsn, "get_open_trades",
                           return_value=[{"trade_id": "bad"}, {"trade_id": "good"}]), \
         mock.patch.object(tpsn, "tp_safety_net_check_trade", side_effect=fake_check):
        asyncio.run(tpsn.tp_safety_net_sweep(_FakeBridge(), {}))
    assert calls == ["bad", "good"]
