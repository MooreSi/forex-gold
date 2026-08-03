"""Characterizes the Risk Governor methods on SimulationEngine (core/engine.py)
before task 020 extracts them -- see
docs/todo/refactor/core-fees-risk-governor-migration/010-*.md.

Uses SimulationEngine.__new__(SimulationEngine) to get a real instance
(correct class-attribute/MRO access for self._RR_BYPASS_SOURCES etc.)
without running __init__, which would construct a live MT5 bridge.
"""
import os
import tempfile
import time
from types import SimpleNamespace

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


@pytest.fixture
def engine(fresh_db):
    return TradingRuntime.__new__(TradingRuntime)


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _insert_signal(sig_id, direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id, direction="BUY", status="open",
                  close_time=None, net_pnl=0.0, sl_moved_to_be=0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, net_pnl, sl_moved_to_be) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10, 0.10, 2390.0,
             status, time.time(), close_time, net_pnl, sl_moved_to_be),
        )


# ── is_trading_paused ─────────────────────────────────────────────────────────

def test_is_trading_paused_false_when_no_pause_set(engine):
    assert engine.is_trading_paused() is False


def test_is_trading_paused_true_when_future_timestamp_set(engine):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    assert engine.is_trading_paused() is True


def test_is_trading_paused_false_when_past_timestamp(engine):
    db.set_app_config("trade_pause_until", str(time.time() - 3600))
    assert engine.is_trading_paused() is False


# ── _price_in_entry_range ─────────────────────────────────────────────────────

# ── _check_pre_trade_filters ──────────────────────────────────────────────────

def test_check_pre_trade_filters_passes_good_rr(engine):
    # TP1 10pts away, SL 10pts away -> 1:1, above the 0.75 minimum
    result = engine._check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is None


def test_check_pre_trade_filters_blocks_bad_rr(engine):
    # TP1 1pt away, SL 10pts away -> 0.1:1, below the 0.75 minimum
    result = engine._check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
    )
    assert result is not None
    assert "R:R filter" in result


def test_check_pre_trade_filters_bypasses_rr_for_gd_vip_source(engine):
    result = engine._check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="Telegram Auto (Gold Diggers VIP)",
    )
    assert result is None


def test_check_pre_trade_filters_directional_cap(engine):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=0)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=0)
    result = engine._check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is not None
    assert "Directional cap" in result


def test_check_pre_trade_filters_directional_cap_ignores_protected_trades(engine):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=1)  # already at BE -- doesn't count
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=1)
    result = engine._check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is None


# ── _rg_day_start_ts ──────────────────────────────────────────────────────────

# ── _rg_size_and_check ────────────────────────────────────────────────────────

# ── _rg_check_halt ────────────────────────────────────────────────────────────

# ── _rg_apply_halts_on_close -- the atomicity gap ─────────────────────────────

def test_rg_apply_halts_on_close_sets_pause_and_reason_together(engine):
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()
    engine._rg_apply_halts_on_close(rs, balance=1000.0)
    assert db.get_app_config("trade_pause_until") is not None
    assert db.get_app_config("risk_halt_reason") is not None


def test_rg_apply_halts_on_close_is_atomic_since_020_fix(engine):
    """Was test_rg_apply_halts_on_close_is_not_atomic_today: documented a real
    atomicity gap where _rg_apply_halts_on_close made two SEPARATE top-level
    set_app_config() calls (trade_pause_until, then risk_halt_reason), each
    its own independent db_module.db() transaction (depth 0->1->0 twice, not
    nested) -- a crash between them could leave the pause flag set with no
    reason, or vice versa. Fixed in the 020 extraction (core_risk_governor.py)
    by wrapping the whole method body in one outer `with db_module.db():`, so
    a failure partway through now rolls back both writes together via the
    connection's single (nested, uncommitted-until-outermost) transaction.
    Now wired in -- this asserts the FIXED atomic behavior takes effect."""
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()

    from unittest.mock import patch
    call_count = {"n": 0}
    real_set_app_config = db.set_app_config

    def _fail_on_second_call(key, value):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash between the two config writes")
        return real_set_app_config(key, value)

    with patch.object(db, "set_app_config", side_effect=_fail_on_second_call):
        with pytest.raises(RuntimeError):
            engine._rg_apply_halts_on_close(rs, balance=1000.0)

    # Fixed: both writes are in the same outer transaction, so the forced
    # failure on the second call rolls back the first one too -- neither
    # survives.
    assert db.get_app_config("trade_pause_until") is None
    assert db.get_app_config("risk_halt_reason") is None
