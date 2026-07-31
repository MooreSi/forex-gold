"""Characterizes the four genuinely-computational blocks embedded inline in
SimulationEngine._monitor_loop (core/engine.py) against UNMODIFIED
engine.py, ahead of extraction into forex_trader/core/core_monitor_loop.py
-- see docs/todo/refactor/core-monitor-loop-migration/010-*.md.

reconcile_sl_hit/check_profit_close_target can close a real MT5 position or
record a partial close -- every order-placing collaborator is faked in
every test here.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.positions import monitor_loop as ml
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
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
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _insert_open_trade(trade_id, direction="BUY", stop_loss=2380.0, mt5_ticket=555,
                       remaining_lots=0.10, entry_price=2400.0, managed_by="python",
                       strategy="scale_out"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, entry_price, entry_price,
             entry_price, 0.10, remaining_lots, stop_loss, "open", time.time(), strategy, managed_by),
        )


def _make_engine(bridge):
    e = SimulationEngine.__new__(SimulationEngine)
    e._monitor_running = True
    e._bridge = bridge
    e._sync_cycle = 0
    e._profit_cycle = 0
    e._cal_cycle = 0
    e._dxy_cycle = 0
    e._dpm_candles = None
    e._dpm_dxy_candles = None
    e._cfg = {}
    e._tp_trigger_cache = _TPCache()
    e._scale_out_last_fail = {}
    e._tp_safety_net_last_alert = {}
    return e


class _FakeBridge:
    def __init__(self, tick, configured=True, positions=None, get_positions_raises=False,
                close_result=None, close_raises=False):
        self._tick = tick
        self._configured = configured
        self._positions = positions if positions is not None else []
        self._raises = get_positions_raises
        self._close_result = close_result or {"success": True, "close_price": 2405.0}
        self._close_raises = close_raises
        self.close_calls = []

    async def get_tick(self):
        return self._tick

    def is_configured(self):
        return self._configured

    async def get_positions(self):
        if self._raises:
            raise RuntimeError("mt5 http down")
        return self._positions

    async def close_position(self, ticket):
        self.close_calls.append(ticket)
        if self._close_raises:
            raise RuntimeError("mt5 down")
        return self._close_result


def _run_one_cycle(bridge, trade_id, profit_close_usd=None, ea_instance=None,
                   ea_get_instance_missing=False):
    if profit_close_usd is not None:
        db.update_risk_settings({"profit_close_usd": profit_close_usd})
    e = _make_engine(bridge)
    record_close_calls, partial_calls, sched_calls, commentary_calls, handler_calls, alerts = \
        [], [], [], [], [], []

    async def fake_record_close(tid, price, reason, ctx):
        record_close_calls.append((tid, price, reason))
        return {"trade_id": tid}

    async def fake_partial_close(tid, lots, price, reason):
        partial_calls.append((tid, lots, price, reason))

    async def fake_sched(self_, tid, ticket):
        sched_calls.append((tid, ticket))

    async def fake_commentary(self_, tid, result, reason, tick):
        commentary_calls.append((tid, reason))

    async def fake_scale_out(self_, trade, tick):
        handler_calls.append(trade["trade_id"])

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    calls = {"c": 0}

    async def stop_sleep(*a, **k):
        calls["c"] += 1
        if calls["c"] >= 1:
            e._monitor_running = False

    patches = [
        mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=stop_sleep)),
        mock.patch.object(ml, "record_close", fake_record_close),
        mock.patch.object(ml, "partial_close_trade", fake_partial_close),
        mock.patch.object(SimulationEngine, "_schedule_profit_sync", fake_sched),
        mock.patch.object(SimulationEngine, "_background_close_commentary", fake_commentary),
        mock.patch.object(SimulationEngine, "_handle_scale_out", fake_scale_out),
        mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send),
    ]
    if not ea_get_instance_missing:
        patches.append(mock.patch.object(ea_bridge, "get_instance", return_value=ea_instance))
    for p in patches:
        p.start()
    try:
        asyncio.run(e._monitor_loop())
    finally:
        for p in patches:
            p.stop()
    with db.db() as conn:
        row = db.row_to_dict(conn.execute(
            "SELECT status, managed_by FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone())
    return {
        "record_close": record_close_calls, "partial": partial_calls, "sched": sched_calls,
        "commentary": commentary_calls, "handler": handler_calls, "alerts": alerts, "row": row,
    }


# ── _check_sl (pure) ─────────────────────────────────────────────────────

def test_check_sl_buy_crosses():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0}
    tick = SimpleNamespace(bid=2389.0, ask=2389.5)
    assert e._check_sl(trade, tick) == ("t1", 2390.0, "SL")


def test_check_sl_sell_crosses():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "SELL", "stop_loss": 2410.0}
    tick = SimpleNamespace(bid=2409.5, ask=2410.0)
    assert e._check_sl(trade, tick) == ("t1", 2410.0, "SL")


def test_check_sl_no_cross():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0}
    tick = SimpleNamespace(bid=2395.0, ask=2395.5)
    assert e._check_sl(trade, tick) is None


def test_check_sl_no_stop_loss():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": None}
    tick = SimpleNamespace(bid=2000.0, ask=2000.5)
    assert e._check_sl(trade, tick) is None


# ── SL-hit MT5 reconciliation ────────────────────────────────────────────

_SL_TICK = SimpleNamespace(bid=2389.0, ask=2389.5)


def test_sl_hit_mt5_still_fully_open_deferred(fresh_db):
    _insert_open_trade("t-defer", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK, positions=[{"ticket": 555, "volume": 0.10}])
    r = _run_one_cycle(bridge, "t-defer")
    assert r["record_close"] == []
    assert r["partial"] == []
    assert r["row"]["status"] == "open"


def test_sl_hit_mt5_partially_closed_records_partial(fresh_db):
    _insert_open_trade("t-partial", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK, positions=[{"ticket": 555, "volume": 0.04}])
    r = _run_one_cycle(bridge, "t-partial")
    assert r["record_close"] == []
    assert r["partial"] == [("t-partial", 0.06, 2390.0, "MT5_SL")]


def test_sl_hit_mt5_ticket_gone_full_close(fresh_db):
    _insert_open_trade("t-full", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK, positions=[])
    r = _run_one_cycle(bridge, "t-full")
    assert r["record_close"] == [("t-full", 2390.0, "SL")]
    assert r["sched"] == [("t-full", 555)]
    assert r["commentary"] == [("t-full", "SL")]


def test_sl_hit_bridge_not_configured_skips_mt5_check(fresh_db):
    _insert_open_trade("t-notconf", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK, configured=False, positions=[{"ticket": 555, "volume": 0.10}])
    r = _run_one_cycle(bridge, "t-notconf")
    assert r["record_close"] == [("t-notconf", 2390.0, "SL")]


def test_sl_hit_get_positions_raises_falls_through_to_local_close(fresh_db):
    _insert_open_trade("t-raise", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK, get_positions_raises=True)
    r = _run_one_cycle(bridge, "t-raise")
    assert r["record_close"] == [("t-raise", 2390.0, "SL")]


def test_sl_hit_no_mt5_ticket_local_close_no_profit_sync(fresh_db):
    _insert_open_trade("t-noticket", stop_loss=2390.0, mt5_ticket=None, remaining_lots=0.10)
    bridge = _FakeBridge(_SL_TICK)
    r = _run_one_cycle(bridge, "t-noticket")
    assert r["record_close"] == [("t-noticket", 2390.0, "SL")]
    assert r["sched"] == []


# ── Profit-close target ──────────────────────────────────────────────────

_PROFIT_TICK = SimpleNamespace(bid=2410.0, ask=2410.5)


def test_profit_target_hit_closes_at_mt5_reported_price(fresh_db):
    _insert_open_trade("t-profit", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    bridge = _FakeBridge(_PROFIT_TICK, close_result={"success": True, "close_price": 2411.0})
    r = _run_one_cycle(bridge, "t-profit", profit_close_usd=1.0)
    assert r["record_close"] == [("t-profit", 2411.0, "profit_close_target")]
    assert bridge.close_calls == [555]


def test_profit_target_not_hit_falls_to_handler_dispatch(fresh_db):
    _insert_open_trade("t-noprofit", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    bridge = _FakeBridge(_PROFIT_TICK)
    r = _run_one_cycle(bridge, "t-noprofit", profit_close_usd=99999.0)
    assert r["record_close"] == []
    assert r["handler"] == ["t-noprofit"]


def test_profit_close_disabled_falls_to_handler_dispatch(fresh_db):
    _insert_open_trade("t-disabled", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    bridge = _FakeBridge(_PROFIT_TICK)
    r = _run_one_cycle(bridge, "t-disabled", profit_close_usd=0.0)
    assert r["record_close"] == []
    assert r["handler"] == ["t-disabled"]


def test_profit_target_hit_mt5_close_rejected_falls_back_to_tick_price(fresh_db):
    _insert_open_trade("t-mt5fail", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    bridge = _FakeBridge(_PROFIT_TICK, close_result={"success": False, "error": "requote"})
    r = _run_one_cycle(bridge, "t-mt5fail", profit_close_usd=1.0)
    assert r["record_close"] == [("t-mt5fail", 2410.0, "profit_close_target")]


def test_profit_target_hit_close_position_raises_falls_back_to_tick_price(fresh_db):
    _insert_open_trade("t-mt5raise", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    bridge = _FakeBridge(_PROFIT_TICK, close_raises=True)
    r = _run_one_cycle(bridge, "t-mt5raise", profit_close_usd=1.0)
    assert r["record_close"] == [("t-mt5raise", 2410.0, "profit_close_target")]


# ── EA handoff reclaim ───────────────────────────────────────────────────

_EA_TICK = SimpleNamespace(bid=2395.0, ask=2395.5)  # no SL hit, no profit target


def test_ea_healthy_skips_reclaim_and_dispatch(fresh_db):
    _insert_open_trade("t-ea-healthy", managed_by="ea")
    bridge = _FakeBridge(_EA_TICK)
    ea = SimpleNamespace(is_ea_healthy=lambda: True)
    r = _run_one_cycle(bridge, "t-ea-healthy", ea_instance=ea)
    assert r["alerts"] == []
    assert r["handler"] == []
    assert r["row"]["managed_by"] == "ea"


def test_ea_unhealthy_reclaims_then_dispatches(fresh_db):
    _insert_open_trade("t-ea-unhealthy", managed_by="ea")
    bridge = _FakeBridge(_EA_TICK)
    ea = SimpleNamespace(is_ea_healthy=lambda: False)
    r = _run_one_cycle(bridge, "t-ea-unhealthy", ea_instance=ea)
    assert len(r["alerts"]) == 1
    assert "EA Bridge Lost" in r["alerts"][0]
    assert r["handler"] == ["t-ea-unhealthy"]
    assert r["row"]["managed_by"] == "python"


def test_ea_instance_none_reclaims_then_dispatches(fresh_db):
    _insert_open_trade("t-ea-none", managed_by="ea")
    bridge = _FakeBridge(_EA_TICK)
    r = _run_one_cycle(bridge, "t-ea-none", ea_instance=None)
    assert len(r["alerts"]) == 1
    assert r["handler"] == ["t-ea-none"]
    assert r["row"]["managed_by"] == "python"
