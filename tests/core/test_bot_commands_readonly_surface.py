"""Proves backend.src.services.telegram.bot_readonly's extracted
functions behave identically to SimulationEngine's originals, characterized
in test_bot_commands_readonly_characterization.py -- see
docs/todo/refactor/core-bot-commands-readonly-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. No real or demo MT5 order is ever placed, closed, or modified --
this cluster has no order-placing surface at all.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import bot_readonly as cmds


class _FakeBridge:
    def __init__(self, account=None, tick=None):
        self._account = account or {}
        self._tick = tick

    async def get_account(self):
        return self._account

    async def get_tick(self):
        return self._tick


def _insert_signal_and_trade(trade_id, direction="BUY", entry_price=2400.0,
                             remaining_lots=0.10, stop_loss=2390.0, tp1=None,
                             mt5_ticket=None, status="open", close_time=None,
                             close_price=None, mt5_profit=None, exit_reason=None,
                             open_time=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, tp1, status, "
            "open_time, close_time, close_price, mt5_profit, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, direction, entry_price, entry_price,
             entry_price, remaining_lots, remaining_lots, stop_loss, tp1, status,
             open_time or time.time(), close_time, close_price, mt5_profit, exit_reason),
        )


# ── cmd_help ──────────────────────────────────────────────────────────────

def test_help_lists_commands(fresh_db):
    result = asyncio.run(cmds.cmd_help([]))
    assert "/balance" in result and "/status" in result and "/help" in result


# ── cmd_balance ───────────────────────────────────────────────────────────

def test_balance_uses_mt5_account_when_available(fresh_db):
    bridge = _FakeBridge(account={"balance": 1234.56, "equity": 1300.0, "margin_free": 1200.0})
    result = asyncio.run(cmds.cmd_balance([], bridge))
    assert "$1,234.56" in result
    assert "MT5" in result


def test_balance_falls_back_to_simulation(fresh_db):
    bridge = _FakeBridge(account={})
    result = asyncio.run(cmds.cmd_balance([], bridge))
    assert "Simulation" in result


def test_balance_includes_open_pnl_when_trades_open(fresh_db):
    bridge = _FakeBridge(account={"balance": 1000.0, "equity": 1000.0, "margin_free": 1000.0},
                         tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10)
    result = asyncio.run(cmds.cmd_balance([], bridge))
    assert "Open P&L:" in result
    assert "1 trade)" in result


# ── cmd_daily ─────────────────────────────────────────────────────────────

def test_daily_no_closed_trades_today(fresh_db):
    result = asyncio.run(cmds.cmd_daily([], _FakeBridge(account={})))
    assert "No closed trades today." in result


def test_daily_aggregates_closed_trades(fresh_db):
    now = time.time()
    _insert_signal_and_trade("t-1", status="closed", close_time=now, close_price=2410.0,
                             mt5_profit=50.0, exit_reason="TP1")
    _insert_signal_and_trade("t-2", status="closed", close_time=now, close_price=2390.0,
                             mt5_profit=-20.0, exit_reason="[SL]")
    result = asyncio.run(cmds.cmd_daily([], _FakeBridge(account={})))
    assert "Net P&L:  +$30.00" in result
    assert "Win rate: 50%  (1W / 1L)" in result


def test_daily_shows_open_trades_section(fresh_db):
    bridge = _FakeBridge(account={}, tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10)
    result = asyncio.run(cmds.cmd_daily([], bridge))
    assert "Open Trades (1)" in result


# ── cmd_status ────────────────────────────────────────────────────────────

def test_status_shows_active_when_not_paused(fresh_db):
    rs = db.get_risk_settings()
    rs["auto_execute_signals"] = 1
    db.update_risk_settings(rs)
    result = asyncio.run(cmds.cmd_status([], _FakeBridge()))
    assert "Trading:      Active" in result


def test_status_shows_paused_state(fresh_db):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    result = asyncio.run(cmds.cmd_status([], _FakeBridge()))
    assert "PAUSED until" in result


def test_status_includes_tg_reader_slots_when_present(fresh_db):
    tg_reader = SimpleNamespace(get_status=lambda: {
        "auth_state": "connected",
        "slots": [{"slot": 1, "group_name": "GD VIP", "listener_active": True}],
    })
    result = asyncio.run(cmds.cmd_status([], _FakeBridge(), tg_reader=tg_reader))
    assert "Slot 1: GD VIP (active)" in result


# ── cmd_trades ────────────────────────────────────────────────────────────

def test_trades_no_open_trades(fresh_db):
    result = asyncio.run(cmds.cmd_trades([], _FakeBridge()))
    assert result == "No open trades."


def test_trades_lists_open_trades(fresh_db):
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10, tp1=2420.0, mt5_ticket=555)
    result = asyncio.run(cmds.cmd_trades([], bridge))
    assert "Open Trade (1)" in result
    assert "Ticket: 555" in result
    assert "TP1: $2420.00" in result


# ── cmd_pause / cmd_resume ───────────────────────────────────────────────

def test_pause_default_one_hour(fresh_db):
    result = asyncio.run(cmds.cmd_pause([]))
    assert "paused for 1h" in result
    until = float(db.get_app_config("trade_pause_until"))
    assert until - time.time() == pytest.approx(3600, abs=5)


def test_pause_explicit_duration(fresh_db):
    result = asyncio.run(cmds.cmd_pause(["30m"]))
    assert "paused for 30m" in result
    until = float(db.get_app_config("trade_pause_until"))
    assert until - time.time() == pytest.approx(1800, abs=5)


def test_pause_invalid_duration_rejected(fresh_db):
    result = asyncio.run(cmds.cmd_pause(["bogus"]))
    assert "Invalid duration" in result
    assert db.get_app_config("trade_pause_until") is None


def test_resume_clears_pause(fresh_db):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    result = asyncio.run(cmds.cmd_resume([]))
    assert db.get_app_config("trade_pause_until") == "0"
    assert "Trading resumed." in result


# ── cmd_risk ──────────────────────────────────────────────────────────────

def test_risk_no_args_reads_current(fresh_db):
    result = asyncio.run(cmds.cmd_risk([], _FakeBridge()))
    assert "Current risk per trade: *0.5%*" in result


def test_risk_valid_value_updates_and_reports_amount(fresh_db):
    bridge = _FakeBridge(account={"balance": 1000.0})
    result = asyncio.run(cmds.cmd_risk(["2"], bridge))
    assert "Risk per trade set to *2.0%*" in result
    assert "$20.00" in result
    assert db.get_risk_settings()["risk_per_trade_pct"] == 2.0


def test_risk_out_of_range_rejected(fresh_db):
    result = asyncio.run(cmds.cmd_risk(["50"], _FakeBridge()))
    assert "must be between" in result
    assert db.get_risk_settings()["risk_per_trade_pct"] == 0.5


def test_risk_non_numeric_rejected(fresh_db):
    result = asyncio.run(cmds.cmd_risk(["abc"], _FakeBridge()))
    assert "Invalid value" in result


# ── cmd_strategy ──────────────────────────────────────────────────────────

def test_strategy_no_args_lists_current(fresh_db):
    result = asyncio.run(cmds.cmd_strategy([]))
    assert "Current strategy:" in result


def test_strategy_alias_resolves_and_updates(fresh_db):
    result = asyncio.run(cmds.cmd_strategy(["ct"]))
    assert "Strategy changed to:" in result
    assert db.get_risk_settings()["trade_strategy"] == "conservative_trial"


def test_strategy_unknown_name_rejected(fresh_db):
    result = asyncio.run(cmds.cmd_strategy(["bogus"]))
    assert "Unknown strategy" in result
    assert db.get_risk_settings()["trade_strategy"] == "scale_out"


# ── DPM/IME toggles ──────────────────────────────────────────────────────

def test_dpm_on_off(fresh_db):
    asyncio.run(cmds.cmd_dpm_on([]))
    assert db.get_risk_settings()["dpm_enabled"] == 1
    asyncio.run(cmds.cmd_dpm_off([]))
    assert db.get_risk_settings()["dpm_enabled"] == 0


def test_ime_on_off(fresh_db):
    asyncio.run(cmds.cmd_ime_on([]))
    assert db.get_risk_settings()["immediate_market_entry"] == 1
    asyncio.run(cmds.cmd_ime_off([]))
    assert db.get_risk_settings()["immediate_market_entry"] == 0
