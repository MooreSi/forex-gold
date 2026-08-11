"""Characterizes the front half of open_trade_from_signal on SimulationEngine
(core/engine.py) -- gates, strategy resolution, and pre-fill SL/lot-size
computation -- before task 020 extracts it into core_signal_resolution.py.
See docs/todo/refactor/core-signal-resolution-migration/010-*.md.

The extraction split point (before the atomic signal-claim) doesn't exist
in current engine.py, so these tests call the FULL open_trade_from_signal
through a fake bridge and read the pre-fill resolved values (`sl`, `lots`)
off the fake's place_order call log -- these are exactly what this pack's
own scope produces, unaffected by the post-fill override branches that run
afterward (pack 13's scope) even though those branches DO immediately
rewrite the DB row for 6 of the 10 strategies. Strategy resolution itself
is read from the returned result["strategy"].

NO real or demo MT5 order is ever placed or modified -- verified via the
fake bridge's own call logs.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_NO_SL_SCALE, STRATEGY_CONSERVATIVE,
    STRATEGY_SCALP_RUNNER, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_TRAIL_STOP,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
)


class _FakeBridge:
    def __init__(self, tick="default", order_result=None, account=None):
        self._tick = _default_tick() if tick == "default" else tick
        self._order_result = order_result or {"ticket": 999, "fill_price": 2400.0}
        self._account = account if account is not None else {"balance": 0}
        self.place_order_calls = []
        self.modify_order_calls = []

    async def get_tick(self):
        return self._tick

    async def get_fresh_tick(self):
        return self._tick

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append(
            {"direction": direction, "lots": lots, "sl": sl, "tp": tp}
        )
        return self._order_result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}

    async def get_account(self):
        return self._account


def _default_tick():
    return SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0)


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    e._dpm_candles = None
    e._cfg = {}
    return e


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

def test_raises_when_signal_not_found(fresh_db, engine):
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "does-not-exist"))


def test_raises_when_signal_wrong_status(fresh_db, engine):
    _insert_signal(status="closed")
    with pytest.raises(ValueError, match="cannot open"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))


def test_raises_when_circuit_breaker_active(fresh_db, engine):
    _insert_signal()
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_active_until": time.time() + 3600,
    })
    with pytest.raises(ValueError, match="Circuit breaker"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_raises_when_session_not_allowed(fresh_db, engine):
    _insert_signal()
    db.update_risk_settings({
        "session_asia_enabled": 0, "session_london_enabled": 0, "session_ny_enabled": 0,
    })
    with pytest.raises(ValueError, match="Trading session"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_raises_when_pretrade_filter_blocks_bad_rr(fresh_db, engine):
    # TP1 1pt from mid vs SL 10pt from mid -> 0.1:1, below the 0.75 min
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2401.0)
    with pytest.raises(ValueError, match="R:R"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_raises_when_price_outside_entry_zone(fresh_db, engine):
    _insert_signal(direction="BUY", entry_low=2399.0, entry_high=2401.0)
    engine._bridge = _FakeBridge(tick=SimpleNamespace(bid=2404.8, ask=2405.2, spread_points=4.0))
    with pytest.raises(ValueError, match="entry zone"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_raises_when_spread_too_wide(fresh_db, engine):
    _insert_signal()
    db.update_fee_settings({"max_allowed_spread_points": 2.0})
    engine._bridge = _FakeBridge(tick=SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0))
    with pytest.raises(ValueError, match="Spread too wide"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_raises_when_channel_paused(fresh_db, engine):
    _insert_signal(source_name="PausedChannel")
    _set_channel_perf("PausedChannel", paused=1)
    with pytest.raises(ValueError, match="paused"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


# ── strategy resolution ────────────────────────────────────────────────────────

def test_strategy_resolution_uses_channel_override(fresh_db, engine):
    _insert_signal(source_name="OverrideChannel")
    db.set_channel_strategy_override("OverrideChannel", STRATEGY_TRAIL_STOP)
    result = asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert result["strategy"] == STRATEGY_TRAIL_STOP


def test_strategy_resolution_uses_auto_rec(fresh_db, engine):
    _insert_signal(source_name="AutoChannel")
    db.set_channel_strategy_override("AutoChannel", None, auto=True)
    db.set_channel_strategy_rec("AutoChannel", STRATEGY_SIGNAL_CLIMBER, "reasoning", 0.9)
    result = asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert result["strategy"] == STRATEGY_SIGNAL_CLIMBER


def test_strategy_resolution_falls_back_to_global_default(fresh_db, engine):
    _insert_signal(source_name="PlainChannel")
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    result = asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert result["strategy"] == STRATEGY_SCALE_OUT


# ── lot sizing ─────────────────────────────────────────────────────────────────

def test_lot_size_override_used_verbatim(fresh_db, engine):
    _insert_signal()
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1", lot_size_override=0.25))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.25


def test_lot_size_from_signal_when_no_override(fresh_db, engine):
    _insert_signal(lot_size=0.33)
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.33


def test_lot_size_risk_based_when_neither_set(fresh_db, engine):
    _insert_signal()  # no lot_size, entry-SL distance 10pt
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    lots = engine._bridge.place_order_calls[0]["lots"]
    assert lots > 0


def test_lot_size_channel_multiplier_applied(fresh_db, engine):
    _insert_signal(source_name="BoostedChannel", lot_size=0.10)
    _set_channel_perf("BoostedChannel", lot_mult=2.0)
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.20


def test_lot_size_channel_multiplier_skipped_for_manual_override(fresh_db, engine):
    _insert_signal(source_name="BoostedChannel")
    _set_channel_perf("BoostedChannel", lot_mult=2.0)
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1", lot_size_override=0.10))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.10


def test_strategy_fixed_lot_wins_over_everything(fresh_db, engine):
    _insert_signal(lot_size=0.10)
    db.update_risk_settings({"strategy_lot_size": 0.77})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1", lot_size_override=0.10))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.77


def test_age_lot_mult_decay_applied(fresh_db, engine):
    _insert_signal(lot_size=0.10)
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1", age_lot_mult=0.5))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.05


def test_age_lot_mult_skipped_for_manual_override(fresh_db, engine):
    _insert_signal()
    asyncio.run(TradingRuntime.open_trade_from_signal(
        engine, "sig-1", lot_size_override=0.10, age_lot_mult=0.5))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.10


# ── per-strategy pre-fill SL ───────────────────────────────────────────────────

def test_scale_out_uses_signal_sl_unchanged(fresh_db, engine):
    _insert_signal(stop_loss=2390.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALE_OUT})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2390.0


def test_no_sl_scale_widens_sl_without_dpm_candles(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0)  # 10pt from mid
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    engine._dpm_candles = None  # ADX gate skipped entirely
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    sl = engine._bridge.place_order_calls[0]["sl"]
    assert sl == 2385.0  # mid(2400) - 10*1.5


def test_no_sl_scale_blocks_when_adx_below_30(fresh_db, engine):
    _insert_signal()
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    engine._dpm_candles = [{"h": 1, "l": 1, "c": 1}]  # non-empty -- ADX gate active
    with patch("backend.src.services.dpm.engine.compute_adx", return_value=15.0):
        with pytest.raises(ValueError, match="ADX"):
            asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_no_sl_scale_allows_when_adx_above_30(fresh_db, engine):
    _insert_signal()
    db.update_risk_settings({"trade_strategy": STRATEGY_NO_SL_SCALE})
    engine._dpm_candles = [{"h": 1, "l": 1, "c": 1}]
    with patch("backend.src.services.dpm.engine.compute_adx", return_value=35.0):
        result = asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert result["strategy"] == STRATEGY_NO_SL_SCALE


def test_conservative_uses_fixed_proxy_sl(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)  # mid=2400
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2395.0  # mid - 5pt


def test_scalp_runner_uses_fixed_proxy_sl(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SCALP_RUNNER})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2390.0  # mid - 10pt


def test_conservative_trial_uses_lot_derived_proxy_sl(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, lot_size=0.10)
    db.update_risk_settings({"trade_strategy": STRATEGY_CONSERVATIVE_TRIAL})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    sl = engine._bridge.place_order_calls[0]["sl"]
    assert sl == 2390.0  # mid - round(100/(0.10*100),1) = mid - 10.0


def test_trail_stop_uses_configured_sl_pts(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_TRAIL_STOP, "trail_stop_sl_pts": 6.0})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2394.0  # mid - 6pt


def test_signal_climber_uses_signal_sl_exactly(fresh_db, engine):
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2385.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_SIGNAL_CLIMBER})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2385.0


def test_reversal_runner_widens_sl(fresh_db, engine):
    # stated dist 5pt -> widened to min(5*4, 20) = 20pt
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=None)
    db.update_risk_settings({"trade_strategy": STRATEGY_REVERSAL_RUNNER})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2380.0  # mid(2400) - 20pt


def test_adaptive_runner_widens_and_caps_sl(fresh_db, engine):
    # stated dist 5pt -> widened 20pt, but capped at 50% of final TP dist (TP=2410, dist=10 -> cap 5pt)
    # max(stated=5, min(widened=20, cap=5)) = max(5, 5) = 5
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2395.0, tp1=2410.0)
    db.update_risk_settings({"trade_strategy": STRATEGY_ADAPTIVE_RUNNER})
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["sl"] == 2395.0  # mid(2400) - 5pt


# ── Risk Governor integration ───────────────────────────────────────────────────

def test_risk_governor_blocks_when_enabled_and_unsafe(fresh_db, engine):
    # 50pt stop, 40pt TP1 -> RR 0.8 (passes the 0.75 pre-trade filter), but at
    # default risk settings (0.5% risk / 1.0% ceiling) on a $1000 balance the
    # risk-correct size for a 50pt stop rounds below 0.01 lots -- RG's own
    # "too small to size" rejection, not the earlier R:R filter.
    _insert_signal(entry_low=2399.0, entry_high=2401.0, stop_loss=2350.0, tp1=2440.0)
    db.update_risk_settings({"risk_governor_enabled": 1})
    with pytest.raises(ValueError, match="Risk Governor blocked"):
        asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls == []


def test_risk_governor_overrides_lot_size_when_allowed(fresh_db, engine):
    # tp1 widened to 2415 (vs default 2410) so RG's own stricter 1.0:1 TP1
    # R:R floor (measured from the live ask/bid, not zone mid) clears
    # comfortably -- the point of this test is the lot-sizing cap, not the
    # R:R gate.
    _insert_signal(lot_size=0.10, tp1=2415.0)
    db.update_risk_settings({
        "risk_governor_enabled": 1, "risk_per_trade_pct": 5.0, "max_risk_per_trade_pct": 5.0,
        "max_lot_size": 0.02,
    })
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    lots = engine._bridge.place_order_calls[0]["lots"]
    assert lots == 0.02  # RG's own max_lot_size cap, not the signal's 0.10


def test_risk_governor_yields_to_strategy_fixed_lot(fresh_db, engine):
    _insert_signal(lot_size=0.10, tp1=2415.0)
    db.update_risk_settings({
        "risk_governor_enabled": 1, "risk_per_trade_pct": 5.0, "max_risk_per_trade_pct": 5.0,
        "max_lot_size": 0.02, "strategy_lot_size": 0.15,
    })
    asyncio.run(TradingRuntime.open_trade_from_signal(engine, "sig-1"))
    assert engine._bridge.place_order_calls[0]["lots"] == 0.15
