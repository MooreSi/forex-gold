"""Confirmation at the level, not just arrival at it.

Section 4.2 of docs/todo/reversal-engine/200. Today a signal fires when price
comes within `PROXIMITY_THRESHOLD_PTS` of a ranked level. Location with no
trigger is why the instant fills lose money: price arriving fast at a level
and being filled immediately is exactly the case where the level is about to
fail, which is the cohort reversal-engine/040 measured at -$2,142.

Every check here is computable from the M1 candles the bridge already serves.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import entry_trigger as et


def c(o, h, l, cl, ts=0.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}


class TestRejection:
    def test_a_buy_is_confirmed_when_price_wicks_below_and_closes_back_above(self):
        candles = [c(3301, 3302, 3297, 3301, ts=1)]
        assert et.rejection(candles, level=3300.0, direction="BUY") is True

    def test_a_buy_is_not_confirmed_while_price_is_still_closing_below(self):
        candles = [c(3301, 3302, 3297, 3298, ts=1)]
        assert et.rejection(candles, level=3300.0, direction="BUY") is False

    def test_a_candle_that_never_reached_the_level_is_not_a_rejection(self):
        """Closing above a level you never traded through is just being
        above it. Without this the check fires on every candle in an
        uptrend."""
        candles = [c(3305, 3306, 3303, 3305, ts=1)]
        assert et.rejection(candles, level=3300.0, direction="BUY") is False

    def test_a_sell_is_the_mirror_image(self):
        assert et.rejection([c(3299, 3303, 3298, 3299, ts=1)],
                            level=3300.0, direction="SELL") is True

    def test_it_reads_the_last_closed_candle_only(self):
        candles = [c(3301, 3302, 3297, 3301, ts=1), c(3301, 3302, 3301, 3302, ts=2)]
        assert et.rejection(candles, level=3300.0, direction="BUY") is False


class TestDeceleration:
    def test_price_slowing_into_the_level_passes(self):
        candles = [c(3290, 3296, 3290, 3296), c(3296, 3299, 3296, 3299),
                   c(3299, 3300, 3299, 3300)]
        assert et.decelerating(candles, bars=2, atr=6.0, max_ratio=0.5) is True

    def test_price_accelerating_into_the_level_fails(self):
        """The instant-fill cohort in one measurement: price arriving fast
        is price that is not going to stop there."""
        candles = [c(3290, 3291, 3290, 3291), c(3291, 3295, 3291, 3295),
                   c(3295, 3305, 3295, 3305)]
        assert et.decelerating(candles, bars=2, atr=6.0, max_ratio=0.5) is False

    def test_a_zero_atr_cannot_decide_and_says_so(self):
        candles = [c(3299, 3300, 3299, 3300)] * 3
        assert et.decelerating(candles, bars=2, atr=0.0) is None


class TestSweepReclaim:
    def test_a_pool_taken_and_reclaimed_within_the_window_confirms(self):
        candles = [c(3302, 3303, 3298, 3299, ts=1),   # sweeps below 3300
                   c(3299, 3302, 3299, 3301, ts=2)]   # reclaims
        assert et.sweep_reclaimed(candles, pool=3300.0, direction="BUY",
                                  within_bars=3) is True

    def test_a_pool_taken_and_not_reclaimed_does_not_confirm(self):
        candles = [c(3302, 3303, 3298, 3299, ts=1),
                   c(3299, 3299.5, 3296, 3297, ts=2)]
        assert et.sweep_reclaimed(candles, pool=3300.0, direction="BUY",
                                  within_bars=3) is False

    def test_a_reclaim_after_the_window_is_too_late(self):
        candles = [c(3302, 3303, 3298, 3299, ts=1),
                   c(3299, 3299.5, 3298, 3299, ts=2),
                   c(3299, 3302, 3299, 3301, ts=3)]
        assert et.sweep_reclaimed(candles, pool=3300.0, direction="BUY",
                                  within_bars=1) is False

    def test_no_sweep_at_all_is_not_a_reclaim(self):
        candles = [c(3302, 3303, 3301, 3302, ts=1), c(3302, 3304, 3302, 3303, ts=2)]
        assert et.sweep_reclaimed(candles, pool=3300.0, direction="BUY",
                                  within_bars=3) is False


class TestTheCombinedGate:
    def _good(self):
        return [c(3294, 3295, 3294, 3295, ts=1),
                c(3295, 3297, 3295, 3297, ts=2),
                c(3297, 3298, 3296, 3301, ts=3)]

    def test_with_every_check_off_it_confirms_anything(self):
        """The default must be byte-identical to today's behaviour: a level
        in proximity, no confirmation required. Nothing trades differently
        until somebody moves a dial."""
        r = et.confirm([], level=3300.0, direction="BUY", atr=6.0,
                       cfg=et.TriggerConfig())
        assert r.passed is True
        assert r.checks == {}

    def test_a_required_check_that_fails_blocks_and_names_itself(self):
        cfg = et.TriggerConfig(require_rejection=True)
        r = et.confirm([c(3305, 3306, 3303, 3305, ts=1)], level=3300.0,
                       direction="BUY", atr=6.0, cfg=cfg)
        assert r.passed is False
        assert r.checks["rejection"] is False
        assert "rejection" in r.reason

    def test_a_check_that_cannot_be_evaluated_does_not_silently_pass(self):
        """An indeterminate check is not a passed check. Treating "no data"
        as "confirmed" is how a gate quietly stops gating."""
        cfg = et.TriggerConfig(require_deceleration=True)
        r = et.confirm([c(3299, 3300, 3299, 3300)] * 3, level=3300.0,
                       direction="BUY", atr=0.0, cfg=cfg)
        assert r.passed is False

    def test_all_required_checks_passing_confirms(self):
        cfg = et.TriggerConfig(require_rejection=True, require_deceleration=True,
                               deceleration_bars=2, max_range_ratio=2.0)
        r = et.confirm(self._good(), level=3300.0, direction="BUY", atr=6.0,
                       cfg=cfg)
        assert r.passed is True
