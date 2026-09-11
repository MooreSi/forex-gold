"""Barriers fitted to volatility, not to a Telegram channel's house style.

Section 1.1 of docs/todo/reversal-engine/200, and the owner's directive of
2026-09-11 that the engine no longer needs to resemble Gold Diggers.

Today `_TP_OFFSETS` is a fixed list of point distances while
`_SL_DIST_BY_SCORE` varies the stop 4-7 points with level quality, so TP1 is
0.75R on a weak level and 0.43R on a strong one: **the better the level, the
worse the payoff.** Both numbers were reverse engineered from the reference
channel's messages.

With ATR barriers on, the stop is a volatility multiple and the ladder is
rescaled around a first target at the same multiple, so R is constant. The
ladder's SHAPE is preserved rather than replaced -- the relative spacing is
the one part of the original geometry that encodes something real about how
far gold travels after a level holds.

Off by default. Every existing signal comes out byte-identical.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import signal_generator as sg


def level(score=0.9, price=3300.0):
    return {"price": price, "score": score, "type": "round_5"}


def context(atr=8.0, **kw):
    ctx = {"atr": atr, "adx": 20.0, "session": "london", "htf_bias": "bullish",
           "h1_bias": "bullish", "price_at_signal": 3300.0}
    ctx.update(kw)
    return ctx


ON = {"enabled": True, "stop_mult": 1.2, "tp1_mult": 1.2}


class TestOffByDefault:
    def test_a_signal_built_with_no_barrier_config_is_unchanged(self):
        """The default must be byte-identical to the constant it replaces.
        A release that silently retunes the strategy is worse than the
        hardcoded value was."""
        before = sg.build_signal(level(), "BUY", context())
        after = sg.build_signal(level(), "BUY",
                                context(atr_barriers={"enabled": False}))
        for key in ("sl_dist", "tp1", "tp8", "stop_loss"):
            assert before[key] == after[key]

    def test_the_strong_level_inversion_still_exists_when_off(self):
        """Stated as a test so that turning the feature off can never be
        mistaken for the problem being fixed. A strong level gets the WIDER
        stop and the same fixed TP1, so its payoff is the worse one."""
        strong = sg.build_signal(level(score=0.95), "BUY", context())
        weak = sg.build_signal(level(score=0.40), "BUY", context())
        assert strong["sl_dist"] > weak["sl_dist"]
        assert strong["rr_tp1"] < weak["rr_tp1"]


class TestOn:
    def test_the_stop_is_a_multiple_of_atr(self):
        sig = sg.build_signal(level(), "BUY", context(atr=8.0, atr_barriers=ON))
        assert sig["sl_dist"] == pytest.approx(9.6)

    def test_the_first_target_is_the_same_multiple_so_tp1_is_one_r(self):
        sig = sg.build_signal(level(), "BUY", context(atr=8.0, atr_barriers=ON))
        assert sig["rr_tp1"] == pytest.approx(1.0)

    def test_level_score_no_longer_changes_the_payoff(self):
        """The inversion, gone. Both levels get the same R because R is now
        defined by volatility rather than by how much the reference channel
        liked the level."""
        strong = sg.build_signal(level(score=0.95), "BUY", context(atr_barriers=ON))
        weak = sg.build_signal(level(score=0.40), "BUY", context(atr_barriers=ON))
        assert strong["rr_tp1"] == pytest.approx(weak["rr_tp1"])

    def test_the_ladders_relative_spacing_survives(self):
        """TP8 was ten times TP1 in the original cascade. It still is: the
        shape is the part of the channel's geometry worth keeping, because
        it encodes how far gold actually runs once a level holds."""
        sig = sg.build_signal(level(), "BUY", context(atr=8.0, atr_barriers=ON))
        d1 = sig["tp1"] - sig["entry_low"] - (sig["entry_high"] - sig["entry_low"]) / 2
        d8 = sig["tp8"] - sig["entry_low"] - (sig["entry_high"] - sig["entry_low"]) / 2
        assert d8 / d1 == pytest.approx(sg._TP_OFFSETS[7] / sg._TP_OFFSETS[0])

    def test_a_quiet_market_produces_a_tighter_stop_than_a_violent_one(self):
        quiet = sg.build_signal(level(), "BUY", context(atr=4.0, atr_barriers=ON))
        wild = sg.build_signal(level(), "BUY", context(atr=16.0, atr_barriers=ON))
        assert wild["sl_dist"] > quiet["sl_dist"]
        assert wild["rr_tp1"] == pytest.approx(quiet["rr_tp1"])

    def test_a_sell_places_its_stop_above_and_targets_below(self):
        sig = sg.build_signal(level(), "SELL", context(atr_barriers=ON))
        mid = (sig["entry_low"] + sig["entry_high"]) / 2
        assert sig["stop_loss"] > mid
        assert sig["tp1"] < mid


class TestItRefusesRatherThanGuesses:
    def test_a_zero_atr_falls_back_to_the_level_score_stop(self):
        """No ATR means no volatility estimate. Sizing a stop off zero would
        produce a stop at the entry price, which is not a tight stop, it is
        an immediate loss."""
        sig = sg.build_signal(level(score=0.95), "BUY",
                              context(atr=0.0, atr_barriers=ON))
        assert sig["sl_dist"] == pytest.approx(sg.sl_distance_for_level(0.95))

    def test_a_nonsense_multiple_is_ignored_rather_than_applied(self):
        cfg = {"enabled": True, "stop_mult": 0.0, "tp1_mult": 1.2}
        sig = sg.build_signal(level(score=0.95), "BUY", context(atr_barriers=cfg))
        assert sig["sl_dist"] == pytest.approx(sg.sl_distance_for_level(0.95))


class TestTheGd2PathIsUntouched:
    def test_a_unicorn_signal_keeps_its_own_confluence_geometry(self):
        """GD2's zone comes from a real FVG/breaker overlap and its targets
        are already R-multiples of that zone's own width. There is no
        inversion there to fix, and overwriting a measured confluence zone
        with an ATR guess would be a downgrade."""
        lvl = {"price": 3300.0, "score": 0.9, "type": "unicorn",
               "profile": "gd2", "entry_zone_low": 3299.0,
               "entry_zone_high": 3301.0}
        on = sg.build_signal(lvl, "BUY", context(atr_barriers=ON))
        off = sg.build_signal(lvl, "BUY", context())
        assert on["sl_dist"] == off["sl_dist"]
        assert on["tp1"] == off["tp1"]
