"""Proves forex_trader.core.core_signal_resolution's extracted function
behaves identically to the front half of SimulationEngine.
open_trade_from_signal, characterized in
test_signal_resolution_characterization.py -- see
docs/todo/refactor/core-signal-resolution-migration/020-*.md.

Unlike 010 (which had to read resolved values off a fake bridge's
place_order call log, since the split didn't exist in engine.py), these
tests call resolve_open_trade_params() directly and assert on its returned
dict -- a more direct equivalent now that the split actually exists.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_signal_resolution as sr
from forex_trader.core import core_strategy_params as sp
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_NO_SL_SCALE, STRATEGY_CONSERVATIVE,
    STRATEGY_SCALP_RUNNER, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_TRAIL_STOP,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
)


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
    sp._cache.clear()
    yield db
    sp._cache.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, tick="default", account=None):
        self._tick = _default_tick() if tick == "default" else tick
        self._account = account if account is not None else {"balance": 0}

    async def get_tick(self):
        return self._tick

    async def get_account(self):
        return self._account


def _default_tick():
    return SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0)


def _insert_signal(sig_id="sig-1", direction="BUY", entry_low=2399.0, entry_high=2401.0,
                   stop_loss=2390.0, tp1=2410.0, status="pending", source_name="TestChannel",
                   lot_size=None, risk_pct=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at, lot_size, risk_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sig_id, source_name, direction, entry_low, entry_high, stop_loss, tp1,
             status, time.time(), lot_size, risk_pct),
        )


def _set_channel_perf(source, lot_mult=1.0, paused=0):
    with db.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO channel_performance (source, lot_mult, paused) "
            "VALUES (?,?,?)",
            (source, lot_mult, paused),
        )


# ── gates ──────────────────────────────────────────────────────────────────────

def test_raises_when_signal_not_found(fresh_db):
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "does-not-exist"))


def test_raises_when_signal_wrong_status(fresh_db):
    _insert_signal(status="closed")
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="cannot open"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_circuit_breaker_active(fresh_db):
    _insert_signal()
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_active_until": time.time() + 3600,
    })
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Circuit breaker"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_session_not_allowed(fresh_db):
    _insert_signal()
    db.update_risk_settings({
        "session_asia_enabled": 0, "session_london_enabled": 0, "session_ny_enabled": 0,
    })
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Trading session"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_pretrade_filter_blocks_bad_rr(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2401.0)
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="R:R"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_price_outside_entry_zone(fresh_db):
    _insert_signal(direction="BUY", entry_low=2399.0, entry_high=2401.0)
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2404.8, ask=2405.2, spread_points=4.0))
    with pytest.raises(ValueError, match="entry zone"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_spread_too_wide(fresh_db):
    _insert_signal()
    db.update_fee_settings({"max_allowed_spread_points": 2.0})
    bridge = _FakeBridge(tick=SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0))
    with pytest.raises(ValueError, match="Spread too wide"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_channel_paused(fresh_db):
    _insert_signal(source_name="PausedChannel")
    _set_channel_perf("PausedChannel", paused=1)
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="paused"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_raises_when_outside_trading_schedule(fresh_db):
    """Proves the gate is actually wired in, not just correct in isolation.
    Enabled with every block left disabled -- blocks regardless of the
    actual wall-clock time the test happens to run at."""
    from forex_trader.core import core_trading_schedule as sched
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(sched._default_schedule())
    _insert_signal()
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Trading Schedule"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


# ── strategy resolution ────────────────────────────────────────────────────────

def test_strategy_resolution_uses_channel_override(fresh_db):
    _insert_signal(source_name="OverrideChannel")
    db.set_channel_strategy_override("OverrideChannel", STRATEGY_TRAIL_STOP)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["strategy"] == STRATEGY_TRAIL_STOP


def test_strategy_resolution_uses_auto_rec(fresh_db):
    _insert_signal(source_name="AutoChannel")
    db.set_channel_strategy_override("AutoChannel", None, auto=True)
    db.set_channel_strategy_rec("AutoChannel", STRATEGY_SIGNAL_CLIMBER, "reasoning", 0.9)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["strategy"] == STRATEGY_SIGNAL_CLIMBER


def test_strategy_resolution_falls_back_to_global_default(fresh_db):
    _insert_signal(source_name="PlainChannel")
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["strategy"] == STRATEGY_SCALE_OUT


# ── EA Templates ─────────────────────────────────────────────────────────────

def test_template_override_resolves_as_template_unmodified_sl(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Grid Stealth", {"mode": "grid", "tpsl_mode": "stealth"})
    _insert_signal(source_name="TplChannel", stop_loss=2390.0)
    db.set_channel_strategy_override("TplChannel", et.override_for_template("Grid Stealth"))
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["is_template"] is True
    assert result["strategy"] == "template:Grid Stealth"
    assert result["template"]["mode"] == "grid"
    assert result["template"]["tpsl_mode"] == "stealth"
    # Templates skip every strategy-specific SL override -- the raw signal
    # SL passes through untouched (the EA computes its own management).
    assert result["stop_loss_to_use"] == 2390.0


def test_template_missing_raises(fresh_db):
    _insert_signal(source_name="TplChannel")
    db.set_channel_strategy_override("TplChannel", "template:DoesNotExist")
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="no longer exists"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_template_sig_guard_blocks_when_trade_already_open(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Guarded", {"sig_guard": True})
    _insert_signal(sig_id="other-sig", source_name="GuardChannel", direction="BUY")
    _insert_signal(source_name="GuardChannel", direction="BUY")
    db.set_channel_strategy_override("GuardChannel", et.override_for_template("Guarded"))
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, entry_low, "
            "entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
            "strategy, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("existing-1", "other-sig", "BUY", 2400.0, 2400.0, 2400.0, 0.10, 0.10, 2390.0,
             "open", time.time(), "template:Guarded", "GuardChannel"),
        )
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Sig Guard"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_template_sig_guard_off_does_not_block(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Unguarded", {"sig_guard": False})
    _insert_signal(sig_id="other-sig", source_name="GuardChannel", direction="BUY")
    _insert_signal(source_name="GuardChannel", direction="BUY")
    db.set_channel_strategy_override("GuardChannel", et.override_for_template("Unguarded"))
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, entry_low, "
            "entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
            "strategy, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("existing-1", "other-sig", "BUY", 2400.0, 2400.0, 2400.0, 0.10, 0.10, 2390.0,
             "open", time.time(), "template:Unguarded", "GuardChannel"),
        )
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["is_template"] is True
    assert result["strategy"] == "template:Unguarded"


# ── lot sizing ─────────────────────────────────────────────────────────────────

def test_lot_size_override_used_verbatim(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1", lot_size_override=0.25))
    assert result["lot_size"] == 0.25


def test_lot_size_from_signal_when_no_override(fresh_db):
    _insert_signal(lot_size=0.33)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["lot_size"] == 0.33


def test_lot_size_risk_based_when_neither_set(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["lot_size"] > 0


def test_lot_size_channel_multiplier_applied(fresh_db):
    _insert_signal(source_name="BoostedChannel", lot_size=0.10)
    _set_channel_perf("BoostedChannel", lot_mult=2.0)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["lot_size"] == 0.20


def test_lot_size_channel_multiplier_skipped_for_manual_override(fresh_db):
    _insert_signal(source_name="BoostedChannel")
    _set_channel_perf("BoostedChannel", lot_mult=2.0)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1", lot_size_override=0.10))
    assert result["lot_size"] == 0.10


def test_strategy_fixed_lot_wins_over_everything(fresh_db):
    _insert_signal(lot_size=0.10)
    db.update_risk_settings({"strategy_lot_size": 0.77})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1", lot_size_override=0.10))
    assert result["lot_size"] == 0.77


def test_age_lot_mult_decay_applied(fresh_db):
    _insert_signal(lot_size=0.10)
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1", age_lot_mult=0.5))
    assert result["lot_size"] == 0.05


def test_age_lot_mult_skipped_for_manual_override(fresh_db):
    _insert_signal()
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(
        bridge, "sig-1", lot_size_override=0.10, age_lot_mult=0.5))
    assert result["lot_size"] == 0.10


# ── per-strategy pre-fill SL ───────────────────────────────────────────────────

def test_scale_out_uses_signal_sl_unchanged(fresh_db):
    _insert_signal(stop_loss=2390.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2390.0


def test_no_sl_scale_widens_sl_without_dpm_candles(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1", dpm_candles=None))
    assert result["stop_loss_to_use"] == 2385.0


def test_no_sl_scale_blocks_when_adx_below_30(fresh_db):
    _insert_signal()
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    bridge = _FakeBridge()
    with patch("forex_trader.core.dpm_engine.compute_adx", return_value=15.0):
        with pytest.raises(ValueError, match="ADX"):
            asyncio.run(sr.resolve_open_trade_params(
                bridge, "sig-1", dpm_candles=[{"h": 1, "l": 1, "c": 1}]))


def test_no_sl_scale_allows_when_adx_above_30(fresh_db):
    _insert_signal()
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    bridge = _FakeBridge()
    with patch("forex_trader.core.dpm_engine.compute_adx", return_value=35.0):
        result = asyncio.run(sr.resolve_open_trade_params(
            bridge, "sig-1", dpm_candles=[{"h": 1, "l": 1, "c": 1}]))
    assert result["strategy"] == STRATEGY_NO_SL_SCALE


def test_conservative_uses_fixed_proxy_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2395.0


def test_scalp_runner_uses_fixed_proxy_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALP_RUNNER})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2390.0


def test_conservative_trial_uses_lot_derived_proxy_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, lot_size=0.10)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE_TRIAL})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2390.0


def test_trail_stop_uses_configured_sl_pts(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_TRAIL_STOP, "trail_stop_sl_pts": 6.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2394.0


def test_signal_climber_uses_signal_sl_exactly(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2385.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SIGNAL_CLIMBER})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2385.0


def test_reversal_runner_widens_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=None)
    db.update_risk_settings({"trade_strategy": STRATEGY_REVERSAL_RUNNER})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2380.0


def test_adaptive_runner_widens_and_caps_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2395.0


def test_adaptive_runner_2_uses_fixed_10pt_sl_ignoring_signal_sl(fresh_db):
    # entry_mid = 2400.0; signal's own stop_loss (2350.0, 50pts away) must be
    # completely ignored -- only the fixed 10pt distance matters.
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2350.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER_2})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2390.0


def test_adaptive_runner_2_sell_direction_sl_is_above_entry(fresh_db):
    _insert_signal(direction="SELL", entry_low=2399.0, entry_high=2401.0,
                   stop_loss=2450.0, tp1=2390.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER_2})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2410.0


# ── Live strategy-parameter overrides (Trading > Strategy > Strategy
# Parameters, core_strategy_params.py) actually reach resolve_open_trade_
# params() -- these are the exact code paths a user editing that panel
# would be changing. ──────────────────────────────────────────────────────

def test_conservative_sl_pt_override_changes_resolved_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE})
    sp.set_strategy_params(STRATEGY_CONSERVATIVE, {"sl_pt": 8.0, "tp1_pt": 3.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2392.0   # entry_mid 2400 - 8


def test_scalp_runner_sl_pt_override_changes_resolved_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALP_RUNNER})
    sp.set_strategy_params(STRATEGY_SCALP_RUNNER, {"sl_pt": 15.0, "tp1_pt": 3.0, "tp2_pt": 4.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2385.0   # entry_mid 2400 - 15


def test_reversal_runner_sl_mult_override_changes_widened_sl(fresh_db):
    # stated_dist = |2400 - 2395| = 5; default 4x -> 20, capped at 20pt = 20.
    # With sl_mult overridden to 2x: min(5*2, cap) = 10.
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=None)
    db.update_risk_settings({"trade_strategy": STRATEGY_REVERSAL_RUNNER})
    sp.set_strategy_params(STRATEGY_REVERSAL_RUNNER, {"sl_mult": 2.0, "sl_cap_pt": 20.0, "sl_floor_pt": 8.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2390.0   # entry_mid 2400 - 10


def test_reversal_runner_sl_floor_override_used_for_bad_data(fresh_db):
    # stop_loss == entry (stated_dist=0) triggers the floor fallback.
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2400.0, tp1=None)
    db.update_risk_settings({"trade_strategy": STRATEGY_REVERSAL_RUNNER})
    sp.set_strategy_params(STRATEGY_REVERSAL_RUNNER, {"sl_mult": 4.0, "sl_cap_pt": 20.0, "sl_floor_pt": 3.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2397.0   # entry_mid 2400 - floor 3


def test_adaptive_runner_tp_cap_frac_override_tightens_sl(fresh_db):
    # stated_dist=5, final_tp_dist=50 (tp1=2450, entry_mid=2400).
    # Default: widened=min(20,20)=20, tp_cap=50*0.5=25 -> min(20,25)=20.
    # With tp_cap_frac=0.10: tp_cap=5 -> max(stated=5, min(20,5))=5.
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=2450.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER})
    sp.set_strategy_params(
        STRATEGY_ADAPTIVE_RUNNER,
        {"sl_mult": 4.0, "sl_cap_pt": 20.0, "sl_floor_pt": 8.0, "tp_cap_frac": 0.10},
    )
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2395.0   # entry_mid 2400 - 5


def test_adaptive_runner_2_sl_pt_override_changes_resolved_sl(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2350.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER_2})
    sp.set_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2, {"sl_pt": 20.0})
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["stop_loss_to_use"] == 2380.0   # entry_mid 2400 - 20


# ── Risk Governor integration ───────────────────────────────────────────────────

def test_risk_governor_blocks_when_enabled_and_unsafe(fresh_db):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2350.0, tp1=2440.0)
    db.update_risk_settings({"risk_governor_enabled": 1})
    bridge = _FakeBridge()
    with pytest.raises(ValueError, match="Risk Governor blocked"):
        asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))


def test_risk_governor_overrides_lot_size_when_allowed(fresh_db):
    _insert_signal(lot_size=0.10, tp1=2415.0)
    db.update_risk_settings({
        "risk_governor_enabled": 1, "risk_per_trade_pct": 5.0, "max_risk_per_trade_pct": 5.0,
        "max_lot_size": 0.02,
    })
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["lot_size"] == 0.02


def test_risk_governor_yields_to_strategy_fixed_lot(fresh_db):
    _insert_signal(lot_size=0.10, tp1=2415.0)
    db.update_risk_settings({
        "risk_governor_enabled": 1, "risk_per_trade_pct": 5.0, "max_risk_per_trade_pct": 5.0,
        "max_lot_size": 0.02, "strategy_lot_size": 0.15,
    })
    bridge = _FakeBridge()
    result = asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))
    assert result["lot_size"] == 0.15
