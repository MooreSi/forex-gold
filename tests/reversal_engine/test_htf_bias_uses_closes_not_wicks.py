"""The higher-timeframe bias must not be vetoed by a single wick.

**bugs/044, found live 2026-09-10 12:55.** Gold fell 60.90 points over fourteen
H1 bars. The account took 28 BUY closes for -$240.45 against 4 SELLs for
+$182.54, and the Reversal Engine's executed trades read bullish-bias BUY at 4
losses to 1 win. `get_htf_bias` returned **neutral** the whole time, and
`htf_bias_blocks` does not block on neutral, so every BUY passed the gate that
exists to prevent exactly this.

The rule required BOTH a lower high and a lower low, measured on **wicks**. On
the live window the second half made a 65.76-point lower low and closed 41.84
points down -- but its highest wick was **3.53 points (0.080%)** above the first
half's, so the lower-high test failed and the verdict was neutral.

**A wick is where price was rejected; a close is where it settled.** Comparing
closes was chosen (owner, 2026-09-10) over adding a tolerance, loosening "both"
to "either", or replacing the structure test with a slope, because it is the
smallest change that needs no new threshold and it agrees with every other
reading of that window: closes, mean-close-per-half, a 0.1% tolerance and
either-condition all said bearish.

**This widens what the gate blocks**, which is the point, and it is a live
trading behaviour change rather than a repair.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine.level_detector import get_htf_bias


def _c(o, h, l, cl):
    return {"open": o, "high": h, "low": l, "close": cl}


def _series(closes, wick=0.5):
    """Candles whose highs/lows hug their closes, so wicks say nothing extra."""
    return [_c(x, x + wick, x - wick, x) for x in closes]


class TestTheLiveCaseThatFoundIt:
    """A decline with one second-half wick poking above the first half's high.
    This is the shape that cost 28 losing BUYs."""

    def _window(self):
        first  = [4400.0, 4410.0, 4420.0, 4415.0, 4405.0,
                  4399.0, 4395.0, 4392.0, 4390.0, 4391.0]
        second = [4388.0, 4380.0, 4370.0, 4360.0, 4350.0,
                  4340.0, 4330.0, 4335.0, 4345.0, 4357.0]
        candles = _series(first) + _series(second)
        # the wick: one second-half bar spikes 3.53 above the first half's max
        # high without its CLOSE going anywhere near it
        candles[10]["high"] = max(c["high"] for c in candles[:10]) + 3.53
        return candles

    def test_a_falling_window_reads_bearish(self):
        assert get_htf_bias(self._window()) == "bearish"

    def test_the_wick_is_genuinely_there(self):
        """Guard the fixture: if the spike stops exceeding the first half's
        high, this test proves nothing about wicks."""
        w = self._window()
        assert max(c["high"] for c in w[10:]) > max(c["high"] for c in w[:10])

    def test_and_the_closes_really_do_fall(self):
        w = self._window()
        assert w[-1]["close"] < w[0]["close"]


class TestItStillDistinguishesTheThreeStates:
    def test_a_clean_uptrend_is_bullish(self):
        assert get_htf_bias(_series([4300 + i * 5 for i in range(20)])) == "bullish"

    def test_a_clean_downtrend_is_bearish(self):
        assert get_htf_bias(_series([4400 - i * 5 for i in range(20)])) == "bearish"

    def test_a_choppy_range_is_still_neutral(self):
        """The gate must not start blocking in a genuine range -- that is what
        'neutral does not block' is for."""
        closes = [4400 + (10 if i % 2 else -10) for i in range(20)]
        assert get_htf_bias(_series(closes)) == "neutral"

    def test_too_few_candles_is_neutral(self):
        assert get_htf_bias(_series([4400, 4401, 4402])) == "neutral"

    def test_no_candles_is_neutral(self):
        assert get_htf_bias([]) == "neutral"


class TestWicksNoLongerDecide:
    def test_an_upward_wick_cannot_veto_a_decline(self):
        """The bug, stated directly."""
        closes = [4400 - i * 4 for i in range(20)]
        clean = _series(closes)
        spiked = _series(closes)
        spiked[12]["high"] = max(c["high"] for c in clean[:10]) + 3.53

        assert get_htf_bias(clean) == "bearish"
        assert get_htf_bias(spiked) == "bearish", (
            "a single wick still flips the verdict to neutral"
        )

    def test_a_downward_wick_cannot_veto_a_rise(self):
        """The same fault in the other direction, which was equally present."""
        closes = [4300 + i * 4 for i in range(20)]
        clean = _series(closes)
        spiked = _series(closes)
        spiked[12]["low"] = min(c["low"] for c in clean[:10]) - 3.53

        assert get_htf_bias(clean) == "bullish"
        assert get_htf_bias(spiked) == "bullish"


class TestBothConditionsAreStillRequired:
    """A mutant loosening `hh and hl` to `hh or hl` survived the first pass.

    An expanding range -- a higher high AND a lower low -- is the case that
    tells them apart, and it is genuinely undecided: the market is making both
    a new high and a new low. Calling that a trend would have the gate blocking
    inside a widening range, which is the over-refusal the runbook warns about.
    """

    def test_an_expanding_range_is_neutral_not_a_trend(self):
        closes = ([4400.0, 4405.0, 4395.0, 4410.0, 4390.0,
                   4402.0, 4398.0, 4404.0, 4396.0, 4400.0]
                  + [4430.0, 4370.0, 4435.0, 4365.0, 4440.0,
                     4360.0, 4445.0, 4355.0, 4442.0, 4358.0])

        assert get_htf_bias(_series(closes)) == "neutral"

    def test_a_contracting_range_is_also_neutral(self):
        """Lower high AND higher low -- the mirror image, equally undecided."""
        closes = ([4340.0, 4460.0, 4345.0, 4455.0, 4350.0,
                   4450.0, 4355.0, 4445.0, 4360.0, 4440.0]
                  + [4395.0, 4405.0, 4396.0, 4404.0, 4397.0,
                     4403.0, 4398.0, 4402.0, 4399.0, 4401.0])

        assert get_htf_bias(_series(closes)) == "neutral"


class TestTheWickFallback:
    """Candles without a `close` must fall back to wicks, NOT read as zero.

    Reading a missing close as 0.0 makes every window neutral, and neutral does
    not block -- which switches the entire gate off silently. Six existing
    tests in test_level_detector.py caught this on the first attempt; these pin
    it deliberately.
    """

    def test_high_low_only_candles_still_produce_a_verdict(self):
        falling = [{"high": 4100.0 - i + 5, "low": 4100.0 - i} for i in range(20)]

        assert get_htf_bias(falling) == "bearish"

    def test_and_the_rising_case_too(self):
        rising = [{"high": 4000.0 + i + 5, "low": 4000.0 + i} for i in range(20)]

        assert get_htf_bias(rising) == "bullish"

    def test_a_zero_close_does_not_read_as_a_real_price(self):
        """A partial payload -- closes present but zero -- must take the same
        fallback rather than comparing zeros."""
        falling = [{"high": 4100.0 - i + 5, "low": 4100.0 - i, "close": 0.0}
                   for i in range(20)]

        assert get_htf_bias(falling) == "bearish"
