"""The EA on-chart panel's market-signal indicators.

`core_panel_signal.py` was 72.7% covered. The gaps were its indicator helpers
-- the ones that decide what the panel tells you about the market.

Two of them exist specifically so the panel cannot contradict the engine:
`_htf_bias` reuses the reversal engine's own `get_htf_bias` when it can, and
`_levels` reads the engine's cache rather than recomputing, so the panel shows
"what the engine would actually trade, not a second opinion". Both fall back
quietly when the engine is not running, and both fallbacks are pinned here --
a fallback nobody has run is a fallback nobody can trust.

Pure functions over candle lists. Nothing here reaches a broker.
"""
from __future__ import annotations

import sys
import types

import pytest

from backend.src.services.positions import core_panel_signal as ps


def _c(o, h, l, cl, vol=0):
    return {"open": o, "high": h, "low": l, "close": cl, "tick_volume": vol}


def _series(n, start=2400.0, step=0.0, vol=0):
    return [_c(start + i * step, start + i * step + 1, start + i * step - 1,
               start + i * step, vol) for i in range(n)]


# ── _vwap ─────────────────────────────────────────────────────────────────────

def test_vwap_is_none_without_candles():
    assert ps._vwap([]) is None


def test_vwap_weights_by_volume():
    """Two bars, the heavier one should pull the average toward itself."""
    candles = [_c(100, 102, 98, 100, vol=1), _c(200, 202, 198, 200, vol=3)]
    # typical prices are 100 and 200; weighted -> (100*1 + 200*3)/4 = 175
    assert ps._vwap(candles) == pytest.approx(175.0)


def test_vwap_falls_back_to_a_plain_mean_when_the_feed_reports_no_volume():
    """Some XAUUSD feeds report tick volume only and a few report zero. An
    unweighted mean is still a usable mean-reversion reference, whereas None
    would blank two panel criteria for no good reason."""
    candles = [_c(100, 102, 98, 100, vol=0), _c(200, 202, 198, 200, vol=0)]
    assert ps._vwap(candles) == pytest.approx(150.0)


# ── _has_displacement ─────────────────────────────────────────────────────────

def test_displacement_needs_enough_history_to_judge():
    assert ps._has_displacement(_series(10)) is False


def test_a_flat_series_has_no_displacement():
    """Every body identical -- nothing separates the recent bars from drift."""
    candles = [_c(100, 101, 99, 100.5) for _ in range(30)]
    assert ps._has_displacement(candles) is False


def test_an_impulsive_recent_candle_is_displacement():
    """The leg that separates a real ICT entry from drift."""
    candles = [_c(100, 101, 99, 100.5) for _ in range(30)]
    candles[-1] = _c(100, 120, 99, 118)          # a body many times the average
    assert ps._has_displacement(candles) is True


def test_an_old_impulsive_candle_does_not_count():
    """It has to be recent -- that is the whole signal."""
    candles = [_c(100, 101, 99, 100.5) for _ in range(30)]
    candles[5] = _c(100, 120, 99, 118)
    assert ps._has_displacement(candles) is False


# ── _active_fvg ───────────────────────────────────────────────────────────────

def test_no_gaps_means_no_active_fvg(monkeypatch):
    monkeypatch.setattr(ps.ict, "detect_fvgs", lambda c: [])
    assert ps._active_fvg(_series(30), 2400.0) == (False, "")


def test_a_filled_gap_is_not_active(monkeypatch):
    """A gap price has already traded back through has done its job and is no
    longer a magnet."""
    monkeypatch.setattr(ps.ict, "detect_fvgs",
                        lambda c: [{"top": 2410, "bottom": 2405,
                                    "filled": True, "direction": "bullish"}])
    assert ps._active_fvg(_series(30), 2400.0) == (False, "")


def test_the_nearest_unfilled_gap_is_the_one_reported(monkeypatch):
    monkeypatch.setattr(ps.ict, "detect_fvgs", lambda c: [
        {"top": 2500, "bottom": 2495, "filled": False, "direction": "bearish"},
        {"top": 2402, "bottom": 2401, "filled": False, "direction": "bullish"},
    ])
    active, direction = ps._active_fvg(_series(30), 2400.0)
    assert active is True
    assert direction == "bullish", "the near gap, not the far one"


def test_a_failing_gap_detector_reports_no_gap_rather_than_raising(monkeypatch):
    monkeypatch.setattr(ps.ict, "detect_fvgs",
                        lambda c: (_ for _ in ()).throw(RuntimeError("bad series")))
    assert ps._active_fvg(_series(30), 2400.0) == (False, "")


# ── _in_order_block ───────────────────────────────────────────────────────────

def test_price_inside_the_block_is_reported(monkeypatch):
    monkeypatch.setattr(ps.ict, "find_breaker_block",
                        lambda c, d: {"low": 2395.0, "high": 2405.0})
    assert ps._in_order_block(_series(30), "BUY", 2400.0) is True


def test_price_outside_the_block_is_not(monkeypatch):
    monkeypatch.setattr(ps.ict, "find_breaker_block",
                        lambda c, d: {"low": 2395.0, "high": 2405.0})
    assert ps._in_order_block(_series(30), "BUY", 2410.0) is False


def test_no_block_at_all_is_false(monkeypatch):
    monkeypatch.setattr(ps.ict, "find_breaker_block", lambda c, d: None)
    assert ps._in_order_block(_series(30), "BUY", 2400.0) is False


def test_a_failing_block_finder_is_false_not_an_error(monkeypatch):
    monkeypatch.setattr(ps.ict, "find_breaker_block",
                        lambda c, d: (_ for _ in ()).throw(RuntimeError("nope")))
    assert ps._in_order_block(_series(30), "BUY", 2400.0) is False


# ── _htf_bias ─────────────────────────────────────────────────────────────────

def _level_detector(monkeypatch, bias=None, raises=False):
    """Patch the ATTRIBUTE on the parent package, not sys.modules.

    `from backend.src.services.reversal_engine import level_detector` resolves
    the attribute off the already-imported parent package, so replacing the
    sys.modules entry does nothing once anything else in the suite has imported
    it. These tests passed alone and failed in a full run until this was fixed.
    """
    import backend.src.services.reversal_engine as pkg

    mod = types.ModuleType("level_detector")

    def get_htf_bias(h1, h4):
        if raises:
            raise RuntimeError("engine unavailable")
        return bias

    mod.get_htf_bias = get_htf_bias
    monkeypatch.setattr(pkg, "level_detector", mod, raising=False)
    monkeypatch.setitem(sys.modules,
                        "backend.src.services.reversal_engine.level_detector", mod)


def test_the_engines_own_bias_is_used_when_available(monkeypatch):
    """So the panel cannot disagree with the engine's level ranking."""
    _level_detector(monkeypatch, bias="bullish")
    assert ps._htf_bias(_series(30), _series(30)) == "BULLISH"


def test_an_unrecognised_engine_answer_falls_through_to_the_simple_read(monkeypatch):
    _level_detector(monkeypatch, bias="sideways-ish")
    rising = _series(30, step=1.0)
    assert ps._htf_bias(rising, rising) == "BULLISH"


def test_the_fallback_runs_when_the_engine_is_not_importable(monkeypatch):
    """A fallback nobody has exercised is a fallback nobody can trust."""
    _level_detector(monkeypatch, raises=True)

    assert ps._htf_bias(_series(30, step=1.0), []) == "BULLISH"
    assert ps._htf_bias(_series(30, start=2500.0, step=-1.0), []) == "BEARISH"


def test_too_little_history_is_neutral_not_a_guess(monkeypatch):
    _level_detector(monkeypatch, raises=True)
    assert ps._htf_bias(_series(5), []) == "NEUTRAL"


# ── _levels ───────────────────────────────────────────────────────────────────

def _engine(monkeypatch, levels=None, instance=True, raises=False):
    mod = types.ModuleType(
        "backend.src.services.reversal_engine.reversal_engine_service")

    def get_instance():
        if raises:
            raise RuntimeError("engine gone")
        if not instance:
            return None
        return types.SimpleNamespace(
            get_status=lambda: {"cached": {"levels": levels or []}})

    mod.get_instance = get_instance
    # Same reason as _level_detector: the attribute on the parent package is
    # what `from ... import reversal_engine_service` actually reads.
    import backend.src.services.reversal_engine as pkg
    monkeypatch.setattr(pkg, "reversal_engine_service", mod, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "backend.src.services.reversal_engine.reversal_engine_service", mod)


def test_no_engine_means_no_levels(monkeypatch):
    _engine(monkeypatch, instance=False)
    assert ps._levels(2400.0) == []


def test_a_broken_engine_lookup_means_no_levels(monkeypatch):
    _engine(monkeypatch, raises=True)
    assert ps._levels(2400.0) == []


def test_levels_come_from_the_engines_cache_with_distance_from_price(monkeypatch):
    """The panel must show what the engine would actually trade, not a second
    opinion -- so the distance is the only thing computed here."""
    _engine(monkeypatch, levels=[
        {"price": 2410.5, "type": "swing_high", "direction": "SELL"},
        {"price": 2390.0, "type": "swing_low", "direction": "BUY"},
    ])

    out = ps._levels(2400.0)

    assert [l["price"] for l in out] == [2410.5, 2390.0]
    assert out[0]["dist"] == pytest.approx(10.5)
    assert out[1]["dist"] == pytest.approx(-10.0)
    assert out[0]["kind"] == "swing_high" and out[0]["dir"] == "SELL"


def test_an_unusable_level_is_skipped_not_shown_as_zero(monkeypatch):
    """A level at 0.00 on the panel would read as a real price."""
    _engine(monkeypatch, levels=[
        {"price": "not-a-number", "type": "x"},
        {"price": 0, "type": "y"},
        {"price": 2405.0, "type": "swing_high"},
    ])

    assert [l["price"] for l in ps._levels(2400.0)] == [2405.0]


def test_only_the_top_six_levels_are_shown(monkeypatch):
    """It is a phone-sized panel."""
    _engine(monkeypatch, levels=[{"price": 2400 + i, "type": "s"} for i in range(20)])
    assert len(ps._levels(2400.0)) == 6
