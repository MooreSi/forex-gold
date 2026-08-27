"""Characterizes the four genuinely-computational blocks embedded inline in
SimulationEngine._monitor_loop (core/engine.py) against UNMODIFIED
engine.py, ahead of extraction into forex_trader/core/core_monitor_loop.py
-- see docs/todo/refactor/core-monitor-loop-migration/010-*.md.

reconcile_sl_hit/check_profit_close_target can close a real MT5 position or
record a partial close -- every order-placing collaborator is faked in
every test here.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest import mock


from backend.src.services.positions.monitor_loop import check_sl

from backend.src.runtime import SimulationEngine

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.positions import monitor_loop as ml
from backend.src.services.positions.monitor_cycle import MonitorState as _MonitorState
from backend.src.services.dpm.bookkeeping import DPMCache as _DPMCache
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from backend.src.runtime import TradingRuntime


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


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
    e = TradingRuntime.__new__(TradingRuntime)
    e._monitor_running = True
    e._bridge = bridge
    # The cycle counters and the DXY candle cache live in a single
    # MonitorState object (M4 B9d) so the monitor cycle -- which lives in
    # services/positions/monitor_cycle.py now -- can mutate them by reference
    # and have the counts survive between cycles. Upstream still sets them as
    # four loose attributes on the engine; the 2026-08-25 merge kept the
    # refactored shape and carried upstream's new pending_revalidate_cycle
    # into MonitorState itself.
    e._monitor_state = _MonitorState()
    e._dpm_candles = None
    e._cfg = {}
    e._tp_trigger_cache = _TPCache()
    e._scale_out_last_fail = {}
    e._tp_safety_net_last_alert = {}
    # _make_monitor_ctx binds every collaborator up front, so the fixture
    # sets what the real __init__ always sets. The inline loop reached for
    # these lazily, on branches these tests never took.
    e._dpm_cache = _DPMCache()
    e._pending_activation_retry_after = {}
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

    async def fake_scale_out(trade, tick, *args, **kwargs):
        # Signature follows the service function the hub now calls directly
        # (trade, tick, bridge, tp_cache, scale_out_last_fail, ...), not the
        # deleted bound wrapper's (self, trade, tick).
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
        mock.patch.object(TradingRuntime, "_schedule_profit_sync", fake_sched),
        mock.patch.object(TradingRuntime, "_background_close_commentary", fake_commentary),
        # M4 B5 inlined the _handle_scale_out wrapper into _monitor_loop, so
        # the sentinel moved to the hub's module-global impl alias. M4 B9d
        # then moved the hub itself into services/positions/monitor_cycle.py,
        # so the alias it calls now lives there. Mock-target relocation only —
        # same function, same signature, new home.
        mock.patch("backend.src.services.positions.monitor_cycle._handle_scale_out_impl",
                   fake_scale_out),
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


# ── check_sl (pure) ──────────────────────────────────────────────────────
# _check_sl was a pure method on the engine; the refactor moved it to
# services/positions/monitor_loop.check_sl as a free function. Same logic,
# same contract -- only the call shape changes. (2026-08-25 merge.)

def test_check_sl_buy_crosses():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0, "entry_price": 2400.0}
    tick = SimpleNamespace(bid=2389.0, ask=2389.5)
    assert check_sl(trade, tick) == ("t1", 2390.0, "SL")


def test_check_sl_sell_crosses():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "SELL", "stop_loss": 2410.0, "entry_price": 2400.0}
    tick = SimpleNamespace(bid=2409.5, ask=2410.0)
    assert check_sl(trade, tick) == ("t1", 2410.0, "SL")


def test_check_sl_no_cross():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0, "entry_price": 2400.0}
    tick = SimpleNamespace(bid=2395.0, ask=2395.5)
    assert check_sl(trade, tick) is None


def test_check_sl_no_stop_loss():
    e = SimulationEngine.__new__(SimulationEngine)
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": None}
    tick = SimpleNamespace(bid=2000.0, ask=2000.5)
    assert check_sl(trade, tick) is None


# ── SL-hit MT5 reconciliation ────────────────────────────────────────────

_SL_TICK = SimpleNamespace(bid=2389.0, ask=2389.5)


# ── Profit-close target ──────────────────────────────────────────────────

_PROFIT_TICK = SimpleNamespace(bid=2410.0, ask=2410.5)


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
