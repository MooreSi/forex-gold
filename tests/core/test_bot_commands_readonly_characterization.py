"""Characterizes the read-only/toggle Telegram bot commands on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-bot-commands-readonly-migration/010-*.md.

No real or demo MT5 order is ever placed, closed, or modified -- this
cluster has no order-placing surface at all. Responses are checked via
substring assertions on the computed values, not full exact-string
matching.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

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
    def __init__(self, account=None, tick=None):
        self._account = account or {}
        self._tick = tick

    async def get_account(self):
        return self._account

    async def get_tick(self):
        return self._tick


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tg_reader = None
    return e


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


# ── _cmd_help ──────────────────────────────────────────────────────────────

def test_help_lists_commands(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_help(engine, []))
    assert "/balance" in result and "/status" in result and "/help" in result


# ── _cmd_balance ──────────────────────────────────────────────────────────

def test_balance_uses_mt5_account_when_available(fresh_db, engine):
    engine._bridge = _FakeBridge(account={"balance": 1234.56, "equity": 1300.0, "margin_free": 1200.0})
    result = asyncio.run(SimulationEngine._cmd_balance(engine, []))
    assert "$1,234.56" in result
    assert "MT5" in result


def test_balance_falls_back_to_simulation(fresh_db, engine):
    engine._bridge = _FakeBridge(account={})
    result = asyncio.run(SimulationEngine._cmd_balance(engine, []))
    assert "Simulation" in result


def test_balance_includes_open_pnl_when_trades_open(fresh_db, engine):
    engine._bridge = _FakeBridge(account={"balance": 1000.0, "equity": 1000.0, "margin_free": 1000.0},
                                 tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10)
    result = asyncio.run(SimulationEngine._cmd_balance(engine, []))
    assert "Open P&L:" in result
    assert "1 trade)" in result


# ── _cmd_daily ────────────────────────────────────────────────────────────

def test_daily_no_closed_trades_today(fresh_db, engine):
    engine._bridge = _FakeBridge(account={})
    result = asyncio.run(SimulationEngine._cmd_daily(engine, []))
    assert "No closed trades today." in result


def test_daily_aggregates_closed_trades(fresh_db, engine):
    engine._bridge = _FakeBridge(account={})
    now = time.time()
    _insert_signal_and_trade("t-1", status="closed", close_time=now, close_price=2410.0,
                             mt5_profit=50.0, exit_reason="TP1")
    _insert_signal_and_trade("t-2", status="closed", close_time=now, close_price=2390.0,
                             mt5_profit=-20.0, exit_reason="[SL]")
    result = asyncio.run(SimulationEngine._cmd_daily(engine, []))
    assert "Net P&L:  +$30.00" in result
    assert "Win rate: 50%  (1W / 1L)" in result


def test_daily_shows_open_trades_section(fresh_db, engine):
    engine._bridge = _FakeBridge(account={}, tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10)
    result = asyncio.run(SimulationEngine._cmd_daily(engine, []))
    assert "Open Trades (1)" in result


# ── _cmd_status ───────────────────────────────────────────────────────────

def test_status_shows_active_when_not_paused(fresh_db, engine):
    rs = db.get_risk_settings()
    rs["auto_execute_signals"] = 1
    db.update_risk_settings(rs)
    result = asyncio.run(SimulationEngine._cmd_status(engine, []))
    assert "Trading:      Active" in result


def test_status_shows_paused_state(fresh_db, engine):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    result = asyncio.run(SimulationEngine._cmd_status(engine, []))
    assert "PAUSED until" in result


def test_status_includes_tg_reader_slots_when_present(fresh_db, engine):
    engine._tg_reader = SimpleNamespace(get_status=lambda: {
        "auth_state": "connected",
        "slots": [{"slot": 1, "group_name": "GD VIP", "listener_active": True}],
    })
    result = asyncio.run(SimulationEngine._cmd_status(engine, []))
    assert "Slot 1: GD VIP (active)" in result


# ── _cmd_trades ───────────────────────────────────────────────────────────

def test_trades_no_open_trades(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_trades(engine, []))
    assert result == "No open trades."


def test_trades_lists_open_trades(fresh_db, engine):
    engine._bridge = _FakeBridge(tick=SimpleNamespace(bid=2410.0, ask=2410.5))
    _insert_signal_and_trade("t-1", entry_price=2400.0, remaining_lots=0.10, tp1=2420.0, mt5_ticket=555)
    result = asyncio.run(SimulationEngine._cmd_trades(engine, []))
    assert "Open Trade (1)" in result
    assert "Ticket: 555" in result
    assert "TP1: $2420.00" in result


# ── _cmd_pause / _cmd_resume ─────────────────────────────────────────────

def test_pause_default_one_hour(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_pause(engine, []))
    assert "paused for 1h" in result
    until = float(db.get_app_config("trade_pause_until"))
    assert until - time.time() == pytest.approx(3600, abs=5)


def test_pause_explicit_duration(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_pause(engine, ["30m"]))
    assert "paused for 30m" in result
    until = float(db.get_app_config("trade_pause_until"))
    assert until - time.time() == pytest.approx(1800, abs=5)


def test_pause_invalid_duration_rejected(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_pause(engine, ["bogus"]))
    assert "Invalid duration" in result
    assert db.get_app_config("trade_pause_until") is None


def test_resume_clears_pause(fresh_db, engine):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    result = asyncio.run(SimulationEngine._cmd_resume(engine, []))
    assert db.get_app_config("trade_pause_until") == "0"
    assert "Trading resumed." in result


# ── _cmd_risk ─────────────────────────────────────────────────────────────

def test_risk_no_args_reads_current(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_risk(engine, []))
    assert "Current risk per trade: *0.5%*" in result


def test_risk_valid_value_updates_and_reports_amount(fresh_db, engine):
    engine._bridge = _FakeBridge(account={"balance": 1000.0})
    result = asyncio.run(SimulationEngine._cmd_risk(engine, ["2"]))
    assert "Risk per trade set to *2.0%*" in result
    assert "$20.00" in result
    assert db.get_risk_settings()["risk_per_trade_pct"] == 2.0


def test_risk_out_of_range_rejected(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_risk(engine, ["50"]))
    assert "must be between" in result
    assert db.get_risk_settings()["risk_per_trade_pct"] == 0.5


def test_risk_non_numeric_rejected(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_risk(engine, ["abc"]))
    assert "Invalid value" in result


# ── _cmd_strategy ─────────────────────────────────────────────────────────

def test_strategy_no_args_lists_current(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_strategy(engine, []))
    assert "Current strategy:" in result


def test_strategy_alias_resolves_and_updates(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_strategy(engine, ["ct"]))
    assert "Strategy changed to:" in result
    assert db.get_risk_settings()["trade_strategy"] == "conservative_trial"


def test_strategy_unknown_name_rejected(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_strategy(engine, ["bogus"]))
    assert "Unknown strategy" in result
    assert db.get_risk_settings()["trade_strategy"] == "scale_out"


# ── DPM/IME toggles ──────────────────────────────────────────────────────

def test_dpm_on_off(fresh_db, engine):
    asyncio.run(SimulationEngine._cmd_dpm_on(engine, []))
    assert db.get_risk_settings()["dpm_enabled"] == 1
    asyncio.run(SimulationEngine._cmd_dpm_off(engine, []))
    assert db.get_risk_settings()["dpm_enabled"] == 0


def test_ime_on_off(fresh_db, engine):
    asyncio.run(SimulationEngine._cmd_ime_on(engine, []))
    assert db.get_risk_settings()["immediate_market_entry"] == 1
    asyncio.run(SimulationEngine._cmd_ime_off(engine, []))
    assert db.get_risk_settings()["immediate_market_entry"] == 0
