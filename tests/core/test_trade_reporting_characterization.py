"""Characterizes get_open_trades/get_all_trades/compute_performance on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-trade-reporting-migration/010-*.md.

get_open_trades/get_all_trades need no `self` -- called via the class
directly. compute_performance reads self._cfg.get("starting_balance", ...),
so it's called via a minimal _FakeEngine stand-in (same pattern as pack 1's
reset_simulation).
"""
import json
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


class _FakeEngine:
    def __init__(self, starting_balance: float):
        self._cfg = {"starting_balance": starting_balance}


def _insert_signal(sig_id="sig-1", direction="BUY", source_name="Manual"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sig_id, source_name, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", status="open",
                  open_time=None, close_time=None, net_pnl=0.0, tg_source=None,
                  claude_open=None, claude_close=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, net_pnl, tg_source, claude_open, claude_close) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10, 0.10, 2390.0,
             status, open_time or time.time(), close_time, net_pnl, tg_source,
             claude_open, claude_close),
        )


# ── get_open_trades ───────────────────────────────────────────────────────────

def test_get_open_trades_returns_only_open_newest_first(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", status="open", open_time=100.0)
    _insert_trade("t-2", "sig-1", status="open", open_time=200.0)
    _insert_trade("t-3", "sig-1", status="closed", open_time=300.0)

    trades = TradingRuntime.get_open_trades(None)
    ids = [t["trade_id"] for t in trades]
    assert ids == ["t-2", "t-1"]


def test_get_open_trades_backfills_tg_source_from_signal(fresh_db):
    _insert_signal("sig-1", source_name="Gold Diggers VIP")
    _insert_trade("t-1", "sig-1", status="open", tg_source=None)

    trades = TradingRuntime.get_open_trades(None)
    assert trades[0]["tg_source"] == "Gold Diggers VIP"


def test_get_open_trades_does_not_overwrite_existing_tg_source(fresh_db):
    _insert_signal("sig-1", source_name="Gold Diggers VIP")
    _insert_trade("t-1", "sig-1", status="open", tg_source="Manual Override")

    trades = TradingRuntime.get_open_trades(None)
    assert trades[0]["tg_source"] == "Manual Override"


def test_get_open_trades_parses_claude_json_columns(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", status="open",
                  claude_open=json.dumps({"take": "bullish"}))

    trades = TradingRuntime.get_open_trades(None)
    assert trades[0]["claude_open"] == {"take": "bullish"}


def test_get_open_trades_leaves_bad_json_alone(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", status="open", claude_open="not json")

    trades = TradingRuntime.get_open_trades(None)
    assert trades[0]["claude_open"] == "not json"


# ── get_all_trades ────────────────────────────────────────────────────────────

# ── compute_performance ───────────────────────────────────────────────────────

def test_compute_performance_no_trades_returns_starting_balance_baseline(fresh_db):
    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["starting_balance"] == 1000.0
    assert perf["current_balance"] == 1000.0
    assert perf["closed_trades"] == 0
    assert perf["win_rate_pct"] == 0.0
    assert perf["profit_factor"] == 0.0


def test_compute_performance_win_rate_and_profit_factor(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", status="closed", open_time=100.0, close_time=200.0, net_pnl=100.0)
    _insert_trade("t-2", "sig-1", status="closed", open_time=100.0, close_time=200.0, net_pnl=-50.0)

    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["closed_trades"] == 2
    assert perf["win_rate_pct"] == 50.0
    assert perf["avg_win"] == 100.0
    assert perf["avg_loss"] == -50.0
    assert perf["profit_factor"] == 2.0
    assert perf["total_net_pnl"] == 50.0


def test_compute_performance_counts_open_trades(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", status="open")
    _insert_trade("t-2", "sig-1", status="open")

    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["open_trades"] == 2


def test_compute_performance_uses_live_sim_account_balance(fresh_db):
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulation_account SET balance=? WHERE id=1", (1234.5,))
    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["current_balance"] == 1234.5
    assert perf["equity"] == 1234.5


def test_compute_performance_peak_balance_falls_back_to_current_when_unset(fresh_db):
    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["peak_balance"] == perf["current_balance"]


def test_compute_performance_peak_balance_reads_app_config_when_set(fresh_db):
    db.set_app_config("peak_balance", "2000.0")
    engine = _FakeEngine(starting_balance=1000.0)
    perf = TradingRuntime.compute_performance(engine)
    assert perf["peak_balance"] == 2000.0
