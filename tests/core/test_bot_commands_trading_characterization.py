"""Characterizes the trading-action Telegram bot commands on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-bot-commands-trading-migration/010-*.md.

close_trade/open_manual_market_order/open_trade/claude_ai.
generate_daily_analysis/email_service.* are all mocked -- already-extracted
(or already-real, stable) collaborators. NO real or demo MT5 order is ever
placed, closed, or modified -- verified via the mocks never being given a
real bridge to act through.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import claude_ai, email_service
from forex_trader.core import core_bot_commands_trading as cmds
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
    async def get_tick(self):
        return None


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._cfg = {}
    e._bridge = _FakeBridge()
    return e


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


# ── _cmd_close ────────────────────────────────────────────────────────────

def test_close_no_open_trades(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_close(engine, []))
    assert result == "No open trades to close."


def test_close_no_args_usage(fresh_db, engine):
    _insert_trade("t-1")
    result = asyncio.run(SimulationEngine._cmd_close(engine, []))
    assert "Usage: /close all" in result


def test_close_all_aggregates_total_pnl(fresh_db, engine):
    _insert_trade("t-1", mt5_ticket=555)
    _insert_trade("t-2", mt5_ticket=556)
    with mock.patch.object(SimulationEngine, "close_trade",
                           new=mock.AsyncMock(return_value={"net_pnl": 20.0, "close_price": 2410.0})) as ct:
        result = asyncio.run(SimulationEngine._cmd_close(engine, ["all"]))
    assert "Closing 2 open trades..." in result
    assert "Done. Total P&L: +$40.00" in result
    assert ct.call_count == 2


def test_close_all_reports_per_trade_failure(fresh_db, engine):
    _insert_trade("t-1", mt5_ticket=555)
    with mock.patch.object(SimulationEngine, "close_trade",
                           new=mock.AsyncMock(side_effect=RuntimeError("mt5 error"))):
        result = asyncio.run(SimulationEngine._cmd_close(engine, ["all"]))
    assert "Failed 555: mt5 error" in result


def test_close_by_valid_ticket(fresh_db, engine):
    _insert_trade("t-1", mt5_ticket=555)
    with mock.patch.object(SimulationEngine, "close_trade",
                           new=mock.AsyncMock(return_value={"net_pnl": -5.0, "close_price": 2395.0})):
        result = asyncio.run(SimulationEngine._cmd_close(engine, ["555"]))
    assert "Closed: BUY 0.1 lots @ $2395.00" in result
    assert "P&L: $-5.00" in result


def test_close_by_unknown_ticket(fresh_db, engine):
    _insert_trade("t-1", mt5_ticket=555)
    result = asyncio.run(SimulationEngine._cmd_close(engine, ["999"]))
    assert result == "No open trade with ticket 999."


def test_close_by_invalid_ticket(fresh_db, engine):
    _insert_trade("t-1", mt5_ticket=555)
    result = asyncio.run(SimulationEngine._cmd_close(engine, ["bogus"]))
    assert "Invalid ticket 'bogus'" in result


# ── _cmd_activate ─────────────────────────────────────────────────────────

def test_activate_no_pending_signal(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_activate(engine, []))
    assert result == "No pending signals to activate."


def test_activate_validation_failure_rejected(fresh_db, engine):
    _insert_tg_signal(entry_low=2410.0, entry_high=2400.0)  # low > high, invalid
    result = asyncio.run(SimulationEngine._cmd_activate(engine, []))
    assert "Signal validation failed" in result


def test_activate_in_zone_opens_trade(fresh_db, engine):
    _insert_tg_signal()
    trade_result = {"trade_id": "trade-abc", "entry_price": 2400.0, "mt5_ticket": 555}
    tick = SimpleNamespace(bid=2399.5, ask=2400.5)
    engine._bridge.get_tick = mock.AsyncMock(return_value=tick)
    with mock.patch.object(cmds, "get_trading_balance", new=mock.AsyncMock(return_value=1000.0)), \
         mock.patch.object(cmds, "open_trade", new=mock.AsyncMock(return_value=trade_result)) as ot:
        result = asyncio.run(SimulationEngine._cmd_activate(engine, []))
    assert "Activated!" in result
    assert "Trade ID: trade-abc" in result
    assert ot.called


def test_activate_outside_zone_saved_pending(fresh_db, engine):
    _insert_tg_signal(entry_low=2399.0, entry_high=2401.0)
    tick = SimpleNamespace(bid=2410.0, ask=2410.5)
    engine._bridge.get_tick = mock.AsyncMock(return_value=tick)
    with mock.patch.object(cmds, "open_trade", new=mock.AsyncMock()) as ot:
        result = asyncio.run(SimulationEngine._cmd_activate(engine, []))
    assert "saved as pending" in result
    assert not ot.called
    with db.db() as conn:
        status = conn.execute("SELECT status FROM vantage_signals").fetchone()[0]
    assert status == "pending"


def test_activate_no_tick_leaves_for_manual(fresh_db, engine):
    _insert_tg_signal()
    engine._bridge.get_tick = mock.AsyncMock(return_value=None)
    with mock.patch.object(cmds, "open_trade", new=mock.AsyncMock()) as ot:
        result = asyncio.run(SimulationEngine._cmd_activate(engine, []))
    assert "no live price available" in result
    assert not ot.called


# ── _cmd_market_price_buy / _cmd_market_price_sell ──────────────────────

def test_market_price_buy_delegates_with_direction(fresh_db, engine):
    result_dict = {"mt5_ticket": 777, "entry_price": 2415.0, "lot_size": 0.05}
    with mock.patch.object(SimulationEngine, "open_manual_market_order",
                           new=mock.AsyncMock(return_value=result_dict)) as om:
        result = asyncio.run(SimulationEngine._cmd_market_price_buy(engine, []))
    om.assert_called_once_with("BUY")
    assert "BUY order placed" in result
    assert "Ticket: 777" in result


def test_market_price_sell_delegates_with_direction(fresh_db, engine):
    result_dict = {"mt5_ticket": 778, "entry_price": 2415.0, "lot_size": 0.05}
    with mock.patch.object(SimulationEngine, "open_manual_market_order",
                           new=mock.AsyncMock(return_value=result_dict)) as om:
        result = asyncio.run(SimulationEngine._cmd_market_price_sell(engine, []))
    om.assert_called_once_with("SELL")
    assert "SELL order placed" in result


def test_market_price_buy_exception_caught_not_raised(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "open_manual_market_order",
                           new=mock.AsyncMock(side_effect=ValueError("no SL configured"))):
        result = asyncio.run(SimulationEngine._cmd_market_price_buy(engine, []))
    assert "Order failed" in result


# ── _cmd_report ───────────────────────────────────────────────────────────

def test_report_no_recipient_configured(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_report(engine, []))
    assert "No recipient email configured" in result


def test_report_send_succeeds(fresh_db, engine):
    ecfg = db.get_email_config()
    ecfg["to_addr"] = "ops@example.com"
    db.save_email_config(ecfg)
    with mock.patch.object(cmds, "compute_mt5_performance",
                           new=mock.AsyncMock(return_value={"balance": 1000.0, "daily_pnl": 10.0})), \
         mock.patch.object(claude_ai, "generate_daily_analysis", new=mock.AsyncMock(return_value="analysis")), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email", new=mock.AsyncMock(return_value=(True, None))):
        result = asyncio.run(SimulationEngine._cmd_report(engine, []))
    assert result == "Report sent to ops@example.com."


def test_report_send_fails(fresh_db, engine):
    ecfg = db.get_email_config()
    ecfg["to_addr"] = "ops@example.com"
    db.save_email_config(ecfg)
    with mock.patch.object(cmds, "compute_mt5_performance", new=mock.AsyncMock(return_value={})), \
         mock.patch.object(claude_ai, "generate_daily_analysis", new=mock.AsyncMock(return_value=None)), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email", new=mock.AsyncMock(return_value=(False, "smtp error"))):
        result = asyncio.run(SimulationEngine._cmd_report(engine, []))
    assert result == "Failed to send report: smtp error"
