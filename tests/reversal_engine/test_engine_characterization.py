"""Characterizes the testable surface of ReversalEngine. Originally written
against engine.py during task 020; re-pointed at reversal_engine_service.py
in task 040 by changing only the import + fixture module (database.py ->
reversal_engine_repo.py, since the service now depends on the repo) -- every
assertion below is unchanged. See
docs/todo/refactor/backend-foundation/020-characterize-reversal-engine-current-behavior.md
and 040-extract-reversal-engine-service-layer.md.

Scope note (unchanged from 020): the async orchestration loops
(_run_cycle, _check_outcomes, _check_correlation) are heavily coupled to
externals this file doesn't attempt to fully mock -- a live MT5 bridge
(self._bridge), the main SimulationEngine (self._main_eng), and
forex_trader.core.database. Covered instead: every pure/isolable method
ReversalEngine exposes, plus the money math and the DB-backed helper methods
(_level_on_cooldown, _already_open, _today_signal_count).
"""
import os
import tempfile
from types import SimpleNamespace

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as db
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    os.remove(path)


@pytest.fixture
def engine(fresh_db):
    return ReversalEngine(bridge=None)


def _tick(bid: float, ask: float, mid: float | None = None, spread: float = 0.0):
    return SimpleNamespace(bid=bid, ask=ask, mid=mid if mid is not None else (bid + ask) / 2, spread=spread)


# ── _realistic_fill ───────────────────────────────────────────────────────────

def test_realistic_fill_buy_entry_fills_at_ask(engine):
    tick = _tick(bid=2400.0, ask=2400.3)
    assert engine._realistic_fill(tick, "BUY", closing=False) == 2400.3


def test_realistic_fill_buy_exit_fills_at_bid(engine):
    tick = _tick(bid=2400.0, ask=2400.3)
    assert engine._realistic_fill(tick, "BUY", closing=True) == 2400.0


def test_realistic_fill_sell_entry_fills_at_bid(engine):
    tick = _tick(bid=2400.0, ask=2400.3)
    assert engine._realistic_fill(tick, "SELL", closing=False) == 2400.0


def test_realistic_fill_sell_exit_fills_at_ask(engine):
    tick = _tick(bid=2400.0, ask=2400.3)
    assert engine._realistic_fill(tick, "SELL", closing=True) == 2400.3


# ── _net_pnl ──────────────────────────────────────────────────────────────────

def test_net_pnl_without_main_engine_returns_gross_pnl_for_both(engine):
    sig = {"trigger_time": 0, "created_at": 0}
    gross, net = engine._net_pnl(sig, pnl_pts=10.0, exit_tick=_tick(2400, 2400.3))
    assert gross == net == 10.0 * 0.1 * 100  # pnl_pts * _VIRTUAL_LOT * 100


def test_net_pnl_with_main_engine_deducts_fees(engine):
    fake_main_eng = SimpleNamespace(
        calculate_fees=lambda lot, spread, hold_hours: {"total_cost": 2.5}
    )
    engine._main_eng = fake_main_eng
    sig = {"trigger_time": 0, "created_at": 0}
    gross, net = engine._net_pnl(sig, pnl_pts=10.0, exit_tick=_tick(2400, 2400.3, spread=0.3))
    assert gross == 100.0
    assert net == 97.5


def test_net_pnl_falls_back_to_gross_when_fee_calc_raises(engine):
    def _boom(*a, **k):
        raise RuntimeError("fee engine unavailable")
    engine._main_eng = SimpleNamespace(calculate_fees=_boom)
    sig = {"trigger_time": 0, "created_at": 0}
    gross, net = engine._net_pnl(sig, pnl_pts=10.0, exit_tick=_tick(2400, 2400.3))
    assert gross == net == 100.0


# ── _calc_atr / _calc_adx ─────────────────────────────────────────────────────

def test_calc_atr_with_insufficient_candles_returns_default(engine):
    assert engine._calc_atr([]) == 8.0
    assert engine._calc_atr([{"high": 1, "low": 1}]) == 8.0


def test_calc_atr_computes_true_range_average():
    candles = [
        {"high": 2410, "low": 2400, "close": 2405},
        {"high": 2415, "low": 2403, "close": 2408},
        {"high": 2420, "low": 2405, "close": 2412},
    ]
    atr = ReversalEngine._calc_atr(candles, period=14)
    assert atr > 0


def test_calc_adx_with_insufficient_candles_returns_default(engine):
    assert engine._calc_adx([{"high": 1, "low": 1}], period=14) == 25.0


# ── _classify_ref_level ───────────────────────────────────────────────────────

def test_classify_ref_level_round_10(engine):
    assert engine._classify_ref_level(2450.5) == "round_10"


def test_classify_ref_level_round_5(engine):
    assert engine._classify_ref_level(2454.5) == "round_5"


def test_classify_ref_level_zero_price_is_unknown(engine):
    assert engine._classify_ref_level(0) == "unknown"


def test_classify_ref_level_matches_cached_level(engine):
    engine._cached["levels"] = [{"price": 2461.0, "type": "asia_high"}]
    assert engine._classify_ref_level(2462.5) == "asia_high"


# ── DB-backed helpers (_level_on_cooldown / _already_open / count) ───────────

def test_level_on_cooldown_true_for_recent_same_direction_nearby_signal(engine, fresh_db):
    fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "s1", "level_price": 2450.0,
    })
    assert engine._level_on_cooldown(2451.0, "BUY") is True   # within 3pt tolerance


def test_level_on_cooldown_false_for_different_direction(engine, fresh_db):
    fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "s1", "level_price": 2450.0,
    })
    assert engine._level_on_cooldown(2451.0, "SELL") is False


def test_level_on_cooldown_false_when_price_far_away(engine, fresh_db):
    fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "s1", "level_price": 2450.0,
    })
    assert engine._level_on_cooldown(2500.0, "BUY") is False


def test_already_open_true_for_open_same_direction_nearby_level(engine, fresh_db):
    fresh_db.create_signal({
        "direction": "SELL", "signal_ref": "s1", "level_price": 2460.0,
    })
    assert engine._already_open("SELL", 2461.5) is True


def test_already_open_false_once_signal_is_closed(engine, fresh_db):
    sig_id = fresh_db.create_signal({
        "direction": "SELL", "signal_ref": "s1", "level_price": 2460.0,
    })
    fresh_db.close_signal(sig_id, 2400.0, "win", net_pnl_dollars=5.0)
    assert engine._already_open("SELL", 2461.5) is False


def test_today_signal_count_matches_db(engine, fresh_db):
    fresh_db.create_signal({"direction": "BUY", "signal_ref": "s1"})
    fresh_db.create_signal({"direction": "SELL", "signal_ref": "s2"})
    assert engine._today_signal_count() == 2


# ── create_signal() column safety ────────────────────────────────────────────
# reversal_engine_repo.create_signal() builds its INSERT column list directly
# from dict.keys() -- any key that isn't a real re_signals column raises
# "no such column" and silently kills every signal-creation cycle. This bit
# _run_cycle's ML-candidate-ranking rework (2026-07-22): ML-only context
# fields (news_proximity_norm, regime_score, etc.) were briefly being set on
# the SAME dict passed to create_signal(), not a separate feat_input copy.

def test_build_signal_output_plus_strategy_is_a_valid_create_signal_dict(engine, fresh_db):
    from backend.src.services.reversal_engine import signal_generator as sg

    level = {"price": 4000.0, "type": "swing_high", "score": 0.8}
    context = {
        "atr": 8.0, "adx": 20.0, "htf_bias": "neutral", "h1_bias": "neutral",
        "session": "london", "price_at_signal": 4000.0,
    }
    sig_data = sg.build_signal(level, "BUY", context)
    sig_data["strategy"] = "scale_out"

    sig_id = fresh_db.create_signal(sig_data)
    assert sig_id


def test_ml_context_fields_are_not_valid_re_signals_columns(fresh_db):
    """Documents exactly which fields must stay out of create_signal()'s
    dict -- these are extract_features() inputs only, never persisted as
    their own columns (the feature VECTOR is stored separately via
    store_ml_features)."""
    ml_only_fields = {
        "news_proximity_norm", "regime_score", "equity_drawdown_pct",
        "ref_discipline_score", "ref_aggression_score",
        "minutes_since_last_ref", "ref_signals_today", "concurrent_agreement",
    }
    for field in ml_only_fields:
        with pytest.raises(Exception, match="has no column named"):
            fresh_db.create_signal({"direction": "BUY", "signal_ref": f"bad-{field}", field: 0.5})


# ── get_status ────────────────────────────────────────────────────────────────

def test_get_status_reflects_running_state(engine):
    status = engine.get_status()
    assert status["is_running"] is False
    assert status["status_msg"] == "Stopped"
    assert "cached" in status
