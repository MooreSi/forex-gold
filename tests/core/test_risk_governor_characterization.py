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

from forex_trader.core import database as db
from forex_trader.core.engine import SimulationEngine


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
    return SimulationEngine.__new__(SimulationEngine)


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

def test_price_in_entry_range_buy_within_zone(engine):
    assert engine._price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2400.0, ask=2400.5)) is True


def test_price_in_entry_range_buy_chasing_above_zone(engine):
    assert engine._price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2402.0, ask=2402.5)) is False


def test_price_in_entry_range_buy_better_fill_below_zone(engine):
    assert engine._price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2398.0, ask=2398.5)) is True


def test_price_in_entry_range_sell_within_zone(engine):
    assert engine._price_in_entry_range("SELL", 2399.0, 2401.0, _tick(bid=2400.0, ask=2400.5)) is True


def test_price_in_entry_range_sell_chasing_below_zone(engine):
    assert engine._price_in_entry_range("SELL", 2399.0, 2401.0, _tick(bid=2398.0, ask=2398.5)) is False


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

def test_rg_day_start_ts_is_in_the_past_and_within_24h(engine):
    ts = engine._rg_day_start_ts()
    now = time.time()
    assert ts <= now
    assert (now - ts) < 86400


# ── _rg_size_and_check ────────────────────────────────────────────────────────

def test_rg_size_and_check_normal_case(engine):
    rs = db.get_risk_settings()  # defaults: risk_per_trade_pct=0.5, max_risk_per_trade_pct=1.0
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert reason is None
    assert lot is not None and lot > 0


def test_rg_size_and_check_rejects_zero_stop_distance(engine):
    rs = db.get_risk_settings()
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2400.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "zero" in reason


def test_rg_size_and_check_rejects_stop_wider_than_atr_cap(engine):
    rs = db.get_risk_settings()
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2380.0,  # 20pt stop
        tp1=2415.0, strategy="scale_out", atr=8.0,  # 1.5x ATR = 12pts, stop is wider
        balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "too wide" in reason


def test_rg_size_and_check_rejects_low_tp1_rr(engine):
    rs = db.get_risk_settings()
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2401.0,  # 1pt TP1 vs 10pt SL
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "R:R" in reason


def test_rg_size_and_check_exempts_gd_vip_runner_from_tp1_rr(engine):
    rs = db.get_risk_settings()
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2401.0,
        strategy="gd_vip_runner", atr=8.0, balance=1000.0, rs=rs,
    )
    assert reason is None
    assert lot is not None


def test_rg_size_and_check_directional_cap(engine):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=0)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=0)
    rs = db.get_risk_settings()
    lot, reason = engine._rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "directional cap" in reason


# ── _rg_check_halt ────────────────────────────────────────────────────────────

def test_rg_check_halt_no_halt_when_within_limits(engine):
    rs = db.get_risk_settings()
    reason = engine._rg_check_halt(rs, balance=1000.0)
    assert reason is None


def test_rg_check_halt_daily_loss_limit(engine):
    _insert_signal("sig-1")
    day_start = engine._rg_day_start_ts()
    _insert_trade("t-1", "sig-1", status="closed", close_time=day_start + 100,
                  net_pnl=-500.0)  # big loss today
    rs = db.get_risk_settings()  # max_daily_loss_pct default 3.0
    reason = engine._rg_check_halt(rs, balance=1000.0)
    assert reason is not None
    assert "Daily loss" in reason


def test_rg_check_halt_total_drawdown_from_peak(engine):
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()  # max_total_drawdown_pct default 8.0
    # balance 1000 vs peak 2000 = 50% drawdown, way above 8%
    reason = engine._rg_check_halt(rs, balance=1000.0)
    assert reason is not None
    assert "Total drawdown" in reason


def test_rg_check_halt_no_drawdown_when_balance_at_or_above_peak(engine):
    db.set_app_config("peak_balance", "1000.0")
    rs = db.get_risk_settings()
    reason = engine._rg_check_halt(rs, balance=1000.0)
    assert reason is None


# ── _rg_apply_halts_on_close -- the atomicity gap ─────────────────────────────

def test_rg_apply_halts_on_close_sets_pause_and_reason_together(engine):
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()
    engine._rg_apply_halts_on_close(rs, balance=1000.0)
    assert db.get_app_config("trade_pause_until") is not None
    assert db.get_app_config("risk_halt_reason") is not None


def test_rg_apply_halts_on_close_is_not_atomic_today(engine):
    """Documents the real (small) atomicity gap in this pack's scope:
    _rg_apply_halts_on_close makes two SEPARATE top-level set_app_config()
    calls (trade_pause_until, then risk_halt_reason), each its own
    independent db_module.db() transaction (depth 0->1->0 twice, not
    nested). A crash between them leaves the pause flag set with no
    reason, or vice versa. Confirmed here with a forced failure; fixed in
    020 by wrapping the whole method body in one outer `with db_module.db():`."""
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

    with patch.object(SimulationEngine._rg_apply_halts_on_close.__globals__["db_module"],
                       "set_app_config", side_effect=_fail_on_second_call):
        with pytest.raises(RuntimeError):
            engine._rg_apply_halts_on_close(rs, balance=1000.0)

    # This is the bug: the first write (trade_pause_until) survived even
    # though the second one (risk_halt_reason) never happened.
    assert db.get_app_config("trade_pause_until") is not None
    assert db.get_app_config("risk_halt_reason") is None
