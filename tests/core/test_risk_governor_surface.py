"""Proves forex_trader.core.core_risk_governor's extracted functions behave
identically to the SimulationEngine methods characterized in
test_risk_governor_characterization.py -- see
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.

Also proves the one real fix in this pack: rg_apply_halts_on_close's two
set_app_config() writes now run inside one transaction (db_module.db() is
thread-local/re-entrant), so a crash between them rolls back BOTH instead
of leaving trade_pause_until set with no risk_halt_reason -- the mirror
image of test_rg_apply_halts_on_close_is_not_atomic_today in 010.
"""
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_risk_governor as rg


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

def test_is_trading_paused_false_when_no_pause_set(fresh_db):
    assert rg.is_trading_paused() is False


def test_is_trading_paused_true_when_future_timestamp_set(fresh_db):
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    assert rg.is_trading_paused() is True


def test_is_trading_paused_false_when_past_timestamp(fresh_db):
    db.set_app_config("trade_pause_until", str(time.time() - 3600))
    assert rg.is_trading_paused() is False


# ── price_in_entry_range ──────────────────────────────────────────────────────

def test_price_in_entry_range_buy_within_zone(fresh_db):
    assert rg.price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2400.0, ask=2400.5)) is True


def test_price_in_entry_range_buy_chasing_above_zone(fresh_db):
    assert rg.price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2402.0, ask=2402.5)) is False


def test_price_in_entry_range_buy_better_fill_below_zone(fresh_db):
    assert rg.price_in_entry_range("BUY", 2399.0, 2401.0, _tick(bid=2398.0, ask=2398.5)) is True


def test_price_in_entry_range_sell_within_zone(fresh_db):
    assert rg.price_in_entry_range("SELL", 2399.0, 2401.0, _tick(bid=2400.0, ask=2400.5)) is True


def test_price_in_entry_range_sell_chasing_below_zone(fresh_db):
    assert rg.price_in_entry_range("SELL", 2399.0, 2401.0, _tick(bid=2398.0, ask=2398.5)) is False


# ── check_pre_trade_filters ───────────────────────────────────────────────────

def test_check_pre_trade_filters_passes_good_rr(fresh_db):
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is None


def test_check_pre_trade_filters_blocks_bad_rr(fresh_db):
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
    )
    assert result is not None
    assert "R:R filter" in result


def test_check_pre_trade_filters_bypasses_rr_for_gd_vip_source(fresh_db):
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="Telegram Auto (Gold Diggers VIP)",
    )
    assert result is None


def test_check_pre_trade_filters_directional_cap(fresh_db):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=0)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=0)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is not None
    assert "Directional cap" in result


# ── rr_filter_bypassed: the IME arm ───────────────────────────────────────────
# Found live 2026-08-06: every GOLD DIGGERS INSTITUTIONAL signal since 08-05
# 12:47 was declined here (TP1 4-6pt against an 8-9pt stop) even though the
# channel runs Immediate Market Entry, so the app opened no GDI trade at all
# while Gold Diggers VIP -- which differs only by being in the static
# RR_BYPASS_SOURCES set -- kept trading. The scan path had already been given
# an IME bypass of its own; the pending-activation watcher and
# resolve_open_trade_params had not, so they still declined the same signals.

def _configure_channel(name, parser_format="gd2", instant_entry_enabled=True):
    db.save_channel_parser_config(
        name, parser_format, "", instant_entry_enabled, True, "test",
    )


def _set_ime(enabled: bool):
    db.update_risk_settings({"immediate_market_entry": 1 if enabled else 0})
    db._rs_cache = None
    db._rs_cache_ts = 0.0


def test_rr_bypassed_for_ime_channel(fresh_db):
    _configure_channel("GOLD DIGGERS INSTITUTIONAL")
    _set_ime(True)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL",
    )
    assert result is None


def test_rr_still_enforced_when_global_ime_is_off(fresh_db):
    """The per-channel flag alone is not enough -- the global toggle gates it,
    exactly as engine.py's own IME gate does."""
    _configure_channel("GOLD DIGGERS INSTITUTIONAL")
    _set_ime(False)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL",
    )
    assert result is not None
    assert "R:R filter" in result


def test_rr_still_enforced_when_channel_opted_out_of_ime(fresh_db):
    _configure_channel("GOLD DIGGERS INSTITUTIONAL", instant_entry_enabled=False)
    _set_ime(True)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL",
    )
    assert result is not None
    assert "R:R filter" in result


def test_rr_still_enforced_for_an_unconfigured_source(fresh_db):
    """A source with no channel_parser_config row at all (the Reversal
    Engine, a manual signal) is not a Telegram channel running IME, so it
    must keep the filter even with the global toggle on."""
    _set_ime(True)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="Reversal Engine",
    )
    assert result is not None
    assert "R:R filter" in result


def test_ime_bypass_does_not_also_waive_the_directional_cap(fresh_db):
    """Filter 2 is a live-exposure limit, not a judgement about the signal's
    levels -- IME says nothing about it and must not disable it."""
    _configure_channel("GOLD DIGGERS INSTITUTIONAL")
    _set_ime(True)
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=0)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=0)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2401.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL",
    )
    assert result is not None
    assert "Directional cap" in result


def test_check_pre_trade_filters_directional_cap_ignores_protected_trades(fresh_db):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=1)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=1)
    result = rg.check_pre_trade_filters(
        "BUY", 2395.0, 2405.0, 2390.0, tp1=2410.0, actual_price=2400.0,
    )
    assert result is None


# ── rg_day_start_ts ───────────────────────────────────────────────────────────

def test_rg_day_start_ts_is_in_the_past_and_within_24h(fresh_db):
    ts = rg.rg_day_start_ts()
    now = time.time()
    assert ts <= now
    assert (now - ts) < 86400


# ── rg_size_and_check ─────────────────────────────────────────────────────────

def test_rg_size_and_check_normal_case(fresh_db):
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert reason is None
    assert lot is not None and lot > 0


def test_rg_size_and_check_rejects_zero_stop_distance(fresh_db):
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2400.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "zero" in reason


def test_rg_size_and_check_rejects_stop_wider_than_atr_cap(fresh_db):
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2380.0,
        tp1=2415.0, strategy="scale_out", atr=8.0,
        balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "too wide" in reason


def test_rg_size_and_check_rejects_low_tp1_rr(fresh_db):
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2401.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "R:R" in reason


def test_rg_size_and_check_exempts_reversal_runner_from_tp1_rr(fresh_db):
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2401.0,
        strategy="reversal_runner", atr=8.0, balance=1000.0, rs=rs,
    )
    assert reason is None
    assert lot is not None


def test_rg_size_and_check_directional_cap(fresh_db):
    _insert_signal("sig-1")
    _insert_signal("sig-2")
    _insert_trade("t-1", "sig-1", direction="BUY", sl_moved_to_be=0)
    _insert_trade("t-2", "sig-2", direction="BUY", sl_moved_to_be=0)
    rs = db.get_risk_settings()
    lot, reason = rg.rg_size_and_check(
        direction="BUY", ref_price=2400.0, stop_loss=2390.0, tp1=2415.0,
        strategy="scale_out", atr=8.0, balance=1000.0, rs=rs,
    )
    assert lot is None
    assert "directional cap" in reason


# ── rg_check_halt ─────────────────────────────────────────────────────────────

def test_rg_check_halt_no_halt_when_within_limits(fresh_db):
    rs = db.get_risk_settings()
    reason = rg.rg_check_halt(rs, balance=1000.0)
    assert reason is None


def test_rg_check_halt_daily_loss_limit(fresh_db):
    _insert_signal("sig-1")
    day_start = rg.rg_day_start_ts()
    _insert_trade("t-1", "sig-1", status="closed", close_time=day_start + 100,
                  net_pnl=-500.0)
    rs = db.get_risk_settings()
    reason = rg.rg_check_halt(rs, balance=1000.0)
    assert reason is not None
    assert "Daily loss" in reason


def test_rg_check_halt_total_drawdown_from_peak(fresh_db):
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()
    reason = rg.rg_check_halt(rs, balance=1000.0)
    assert reason is not None
    assert "Total drawdown" in reason


def test_rg_check_halt_no_drawdown_when_balance_at_or_above_peak(fresh_db):
    db.set_app_config("peak_balance", "1000.0")
    rs = db.get_risk_settings()
    reason = rg.rg_check_halt(rs, balance=1000.0)
    assert reason is None


# ── rg_apply_halts_on_close -- the atomicity fix ──────────────────────────────

def test_rg_apply_halts_on_close_sets_pause_and_reason_together(fresh_db):
    db.set_app_config("peak_balance", "2000.0")
    rs = db.get_risk_settings()
    rg.rg_apply_halts_on_close(rs, balance=1000.0)
    assert db.get_app_config("trade_pause_until") is not None
    assert db.get_app_config("risk_halt_reason") is not None


def test_rg_apply_halts_on_close_is_now_atomic(fresh_db):
    """Mirror of 010's test_rg_apply_halts_on_close_is_not_atomic_today,
    which proved the bug on the original two-separate-calls code. Same
    forced failure here on the extracted function -- because the body is
    now wrapped in one outer `with db_module.db():`, the two inner
    set_app_config() calls share that transaction, so a crash on the
    second call must roll back the first one too."""
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

    with patch.object(rg.rg_apply_halts_on_close.__globals__["db_module"],
                       "set_app_config", side_effect=_fail_on_second_call):
        with pytest.raises(RuntimeError):
            rg.rg_apply_halts_on_close(rs, balance=1000.0)

    # Fixed: neither write survives -- the whole call rolled back together.
    assert db.get_app_config("trade_pause_until") is None
    assert db.get_app_config("risk_halt_reason") is None
