"""Proves backend.src.services.positions.monitor_loop's extracted functions behave
identically to SimulationEngine's originals, characterized in
test_monitor_loop_characterization.py -- see
docs/todo/refactor/core-monitor-loop-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. Every order-placing collaborator is faked in every test here.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.close_trade import CloseTradeContext
from backend.src.services.positions import monitor_loop as ml


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
                       strategy="scale_out", realised_pnl=0.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, strategy, managed_by, realised_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, entry_price, entry_price,
             entry_price, 0.10, remaining_lots, stop_loss, "open", time.time(), strategy,
             managed_by, realised_pnl),
        )


def _trade_row(trade_id):
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone())


class _FakeBridge:
    def __init__(self, configured=True, positions=None, get_positions_raises=False,
                close_result=None, close_raises=False):
        self._configured = configured
        self._positions = positions if positions is not None else []
        self._raises = get_positions_raises
        self._close_result = close_result or {"success": True, "close_price": 2405.0}
        self._close_raises = close_raises
        self.close_calls = []

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


def _make_ctx(bridge):
    record_close_calls, sched_calls, commentary_calls = [], [], []

    async def sched(trade_id, ticket):
        sched_calls.append((trade_id, ticket))

    async def commentary(trade_id, result, reason, tick):
        commentary_calls.append((trade_id, reason))

    ctx = CloseTradeContext(bridge, schedule_profit_sync=sched, background_close_commentary=commentary)
    return ctx, sched_calls, commentary_calls


# ── check_sl (pure) ──────────────────────────────────────────────────────

def test_check_sl_buy_crosses():
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0}
    tick = SimpleNamespace(bid=2389.0, ask=2389.5)
    assert ml.check_sl(trade, tick) == ("t1", 2390.0, "SL")


def test_check_sl_sell_crosses():
    trade = {"trade_id": "t1", "direction": "SELL", "stop_loss": 2410.0}
    tick = SimpleNamespace(bid=2409.5, ask=2410.0)
    assert ml.check_sl(trade, tick) == ("t1", 2410.0, "SL")


def test_check_sl_no_cross():
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": 2390.0}
    tick = SimpleNamespace(bid=2395.0, ask=2395.5)
    assert ml.check_sl(trade, tick) is None


def test_check_sl_no_stop_loss():
    trade = {"trade_id": "t1", "direction": "BUY", "stop_loss": None}
    tick = SimpleNamespace(bid=2000.0, ask=2000.5)
    assert ml.check_sl(trade, tick) is None


# ── reconcile_sl_hit ─────────────────────────────────────────────────────

_SL_TICK = SimpleNamespace(bid=2389.0, ask=2389.5)


def test_sl_hit_mt5_still_fully_open_deferred(fresh_db):
    _insert_open_trade("t-defer", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    trade = _trade_row("t-defer")
    bridge = _FakeBridge(positions=[{"ticket": 555, "volume": 0.10}])
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    with mock.patch("backend.src.services.positions.monitor_loop.partial_close_trade") as mock_partial, \
         mock.patch("backend.src.services.positions.monitor_loop.record_close") as mock_record:
        outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "deferred"
    mock_partial.assert_not_called()
    mock_record.assert_not_called()


def test_sl_hit_mt5_partially_closed_records_partial(fresh_db):
    _insert_open_trade("t-partial", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    trade = _trade_row("t-partial")
    bridge = _FakeBridge(positions=[{"ticket": 555, "volume": 0.04}])
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    partial_calls = []

    async def fake_partial(tid, lots, price, reason):
        partial_calls.append((tid, lots, price, reason))

    with mock.patch("backend.src.services.positions.monitor_loop.partial_close_trade", side_effect=fake_partial), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "partial"
    assert partial_calls == [("t-partial", 0.06, 2390.0, "MT5_SL")]


def test_sl_hit_mt5_ticket_gone_full_close(fresh_db):
    _insert_open_trade("t-full", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    trade = _trade_row("t-full")
    bridge = _FakeBridge(positions=[])
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "closed"
    assert sched_calls == [("t-full", 555)]
    assert commentary_calls == [("t-full", "SL")]
    assert _trade_row("t-full")["status"] == "closed"


def test_sl_hit_bridge_not_configured_skips_mt5_check(fresh_db):
    _insert_open_trade("t-notconf", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    trade = _trade_row("t-notconf")
    bridge = _FakeBridge(configured=False, positions=[{"ticket": 555, "volume": 0.10}])
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "closed"


def test_sl_hit_get_positions_raises_falls_through_to_local_close(fresh_db):
    _insert_open_trade("t-raise", stop_loss=2390.0, mt5_ticket=555, remaining_lots=0.10)
    trade = _trade_row("t-raise")
    bridge = _FakeBridge(get_positions_raises=True)
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "closed"


def test_sl_hit_no_mt5_ticket_local_close_no_profit_sync(fresh_db):
    _insert_open_trade("t-noticket", stop_loss=2390.0, mt5_ticket=None, remaining_lots=0.10)
    trade = _trade_row("t-noticket")
    bridge = _FakeBridge()
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    outcome = asyncio.run(ml.reconcile_sl_hit(trade, _SL_TICK, 2390.0, "SL", bridge, ctx))
    assert outcome == "closed"
    assert sched_calls == []


# ── check_profit_close_target ────────────────────────────────────────────

_PROFIT_TICK = SimpleNamespace(bid=2410.0, ask=2410.5)


def test_profit_target_hit_closes_at_mt5_reported_price(fresh_db):
    _insert_open_trade("t-profit", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    trade = _trade_row("t-profit")
    bridge = _FakeBridge(close_result={"success": True, "close_price": 2411.0})
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    closed = asyncio.run(ml.check_profit_close_target(trade, _PROFIT_TICK, 1.0, bridge, ctx))
    assert closed is True
    assert bridge.close_calls == [555]
    assert _trade_row("t-profit")["close_price"] == 2411.0


def test_profit_target_not_hit_returns_false(fresh_db):
    _insert_open_trade("t-noprofit", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    trade = _trade_row("t-noprofit")
    bridge = _FakeBridge()
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    closed = asyncio.run(ml.check_profit_close_target(trade, _PROFIT_TICK, 99999.0, bridge, ctx))
    assert closed is False
    assert bridge.close_calls == []


def test_profit_close_disabled_returns_false(fresh_db):
    _insert_open_trade("t-disabled", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    trade = _trade_row("t-disabled")
    bridge = _FakeBridge()
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    closed = asyncio.run(ml.check_profit_close_target(trade, _PROFIT_TICK, 0.0, bridge, ctx))
    assert closed is False


def test_profit_target_hit_mt5_close_rejected_falls_back_to_tick_price(fresh_db):
    _insert_open_trade("t-mt5fail", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    trade = _trade_row("t-mt5fail")
    bridge = _FakeBridge(close_result={"success": False, "error": "requote"})
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    closed = asyncio.run(ml.check_profit_close_target(trade, _PROFIT_TICK, 1.0, bridge, ctx))
    assert closed is True
    assert _trade_row("t-mt5fail")["close_price"] == 2410.0


def test_profit_target_hit_close_position_raises_falls_back_to_tick_price(fresh_db):
    _insert_open_trade("t-mt5raise", stop_loss=2380.0, mt5_ticket=555, remaining_lots=0.10, entry_price=2400.0)
    trade = _trade_row("t-mt5raise")
    bridge = _FakeBridge(close_raises=True)
    ctx, sched_calls, commentary_calls = _make_ctx(bridge)
    closed = asyncio.run(ml.check_profit_close_target(trade, _PROFIT_TICK, 1.0, bridge, ctx))
    assert closed is True
    assert _trade_row("t-mt5raise")["close_price"] == 2410.0


# ── reclaim_ea_managed_trade ─────────────────────────────────────────────

def test_ea_healthy_returns_true_no_reclaim(fresh_db):
    _insert_open_trade("t-ea-healthy", managed_by="ea")
    trade = _trade_row("t-ea-healthy")
    ea = SimpleNamespace(is_ea_healthy=lambda: True)
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    with mock.patch.object(ea_bridge, "get_instance", return_value=ea), \
         mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        result = asyncio.run(ml.reclaim_ea_managed_trade(trade, "scale_out"))
    assert result is True
    assert alerts == []
    assert _trade_row("t-ea-healthy")["managed_by"] == "ea"


def test_ea_unhealthy_reclaims_returns_false(fresh_db):
    _insert_open_trade("t-ea-unhealthy", managed_by="ea")
    trade = _trade_row("t-ea-unhealthy")
    ea = SimpleNamespace(is_ea_healthy=lambda: False)
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    with mock.patch.object(ea_bridge, "get_instance", return_value=ea), \
         mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        result = asyncio.run(ml.reclaim_ea_managed_trade(trade, "scale_out"))
    assert result is False
    assert len(alerts) == 1
    assert "EA Bridge Lost" in alerts[0]
    assert _trade_row("t-ea-unhealthy")["managed_by"] == "python"


def test_ea_instance_none_reclaims_returns_false(fresh_db):
    _insert_open_trade("t-ea-none", managed_by="ea")
    trade = _trade_row("t-ea-none")
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    with mock.patch.object(ea_bridge, "get_instance", return_value=None), \
         mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        result = asyncio.run(ml.reclaim_ea_managed_trade(trade, "scale_out"))
    assert result is False
    assert len(alerts) == 1
    assert _trade_row("t-ea-none")["managed_by"] == "python"
