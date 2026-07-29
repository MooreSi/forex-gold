"""Proves forex_trader.core.core_bot_commands_trading's extracted
functions behave identically to SimulationEngine's originals, characterized
in test_bot_commands_trading_characterization.py -- see
docs/todo/refactor/core-bot-commands-trading-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
close_trade/open_manual_market_order/open_trade are mocked in every test.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_bot_commands_trading as cmds


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
    async def get_tick(self):
        return None


def _insert_trade(trade_id, direction="BUY", lot_size=0.10, mt5_ticket=555):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, 2400.0, 2400.0, 2400.0,
             lot_size, lot_size, 2390.0, "open", time.time()),
        )


def _insert_tg_signal(tg_id="tg-1", direction="BUY", entry_low=2399.0, entry_high=2401.0,
                      sl=2390.0, tp1=2410.0, status="new"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,group_name,sender_name,"
            "message_ts,raw_text,parsed_at,direction,entry_low,entry_high,stop_loss,tp1,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tg_id, "grp", "Chan", "sender", "", "text", time.time(), direction,
             entry_low, entry_high, sl, tp1, status),
        )


# ── cmd_activate ──────────────────────────────────────────────────────────

def test_activate_no_pending_signal(fresh_db):
    result = asyncio.run(cmds.cmd_activate([], _FakeBridge()))
    assert result == "No pending signals to activate."


def test_activate_validation_failure_rejected(fresh_db):
    _insert_tg_signal(entry_low=2410.0, entry_high=2400.0)
    result = asyncio.run(cmds.cmd_activate([], _FakeBridge()))
    assert "Signal validation failed" in result


def test_activate_in_zone_opens_trade(fresh_db):
    _insert_tg_signal()
    trade_result = {"trade_id": "trade-abc", "entry_price": 2400.0, "mt5_ticket": 555}
    tick = SimpleNamespace(bid=2399.5, ask=2400.5)

    class _TickBridge(_FakeBridge):
        async def get_tick(self):
            return tick

    with mock.patch.object(cmds, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(cmds, "open_trade", new=mock.AsyncMock(return_value=trade_result)) as ot:
        result = asyncio.run(cmds.cmd_activate([], _TickBridge()))
    assert "Activated!" in result
    assert "Trade ID: trade-abc" in result
    assert ot.called


def test_activate_outside_zone_saved_pending(fresh_db):
    _insert_tg_signal(entry_low=2399.0, entry_high=2401.0)
    tick = SimpleNamespace(bid=2410.0, ask=2410.5)

    class _TickBridge(_FakeBridge):
        async def get_tick(self):
            return tick

    with mock.patch.object(cmds, "open_trade", new=mock.AsyncMock()) as ot:
        result = asyncio.run(cmds.cmd_activate([], _TickBridge()))
    assert "saved as pending" in result
    assert not ot.called
    with db.db() as conn:
        status = conn.execute("SELECT status FROM vantage_signals").fetchone()[0]
    assert status == "pending"


def test_activate_no_tick_leaves_for_manual(fresh_db):
    _insert_tg_signal()
    with mock.patch.object(cmds, "open_trade", new=mock.AsyncMock()) as ot:
        result = asyncio.run(cmds.cmd_activate([], _FakeBridge()))
    assert "no live price available" in result
    assert not ot.called


# ── cmd_report ────────────────────────────────────────────────────────────

def test_report_no_recipient_configured(fresh_db):
    result = asyncio.run(cmds.cmd_report([], _FakeBridge(), {}))
    assert "No recipient email configured" in result


def test_report_send_succeeds(fresh_db):
    from forex_trader.core import claude_ai, email_service
    ecfg = db.get_email_config()
    ecfg["to_addr"] = "ops@example.com"
    db.save_email_config(ecfg)
    with mock.patch.object(cmds, "compute_mt5_performance",
                           new=mock.AsyncMock(return_value={"balance": 1000.0, "daily_pnl": 10.0})), \
         mock.patch.object(claude_ai, "generate_daily_analysis", new=mock.AsyncMock(return_value="analysis")), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email", new=mock.AsyncMock(return_value=(True, None))):
        result = asyncio.run(cmds.cmd_report([], _FakeBridge(), {}))
    assert result == "Report sent to ops@example.com."


def test_report_send_fails(fresh_db):
    from forex_trader.core import claude_ai, email_service
    ecfg = db.get_email_config()
    ecfg["to_addr"] = "ops@example.com"
    db.save_email_config(ecfg)
    with mock.patch.object(cmds, "compute_mt5_performance", new=mock.AsyncMock(return_value={})), \
         mock.patch.object(claude_ai, "generate_daily_analysis", new=mock.AsyncMock(return_value=None)), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email", new=mock.AsyncMock(return_value=(False, "smtp error"))):
        result = asyncio.run(cmds.cmd_report([], _FakeBridge(), {}))
    assert result == "Failed to send report: smtp error"
