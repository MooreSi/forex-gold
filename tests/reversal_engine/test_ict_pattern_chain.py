"""The ICT chain the Reversal Engine actually trades on.

`find_unicorn_setup` is four decisions in a row -- a liquidity sweep, a market
structure shift, a matching unfilled FVG, and a confluence zone -- and its
output becomes a real order on the only engine in this app with live execution
turned on. The module sat at 56.6% coverage with the whole chain from
`detect_equal_levels` downward untested.

Every function here is pure: candles in, a dict or a bool out. No bridge, no
database, no engine. So there is no reason for them to be untested other than
that nobody had done it.

One of these tests records behaviour that is WRONG. It is marked, and it is
`docs/todo/bugs/047`.
"""
from __future__ import annotations

from backend.src.services.reversal_engine import ict_patterns as ict


def _c(ts, o, h, l, cl):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl}


def _flat(n, high=105.0, low=95.0):
    """A quiet range: no pools, no gaps, nothing for any detector to find."""
    return [_c(i * 900, 100.0, high, low + (i % 3) * 0.1, 100.0) for i in range(n)]


def _swept_and_shifted():
    """The full bearish scenario, stage by stage:

    * two near-equal highs at 110.0 and 110.4 -> an eq_high liquidity pool;
    * one candle wicks to 112 and closes back at 108 -> the sweep;
    * price drops through the prior range -> the structure shift;
    * the drop leaves a bearish fair-value gap that nothing later trades back
      into -> the entry zone.
    """
    cs = _flat(15)
    cs[5] = _c(5 * 900, 100.0, 110.0, 95.0, 100.0)
    cs[10] = _c(10 * 900, 100.0, 110.4, 95.2, 100.0)
    cs += [
        _c(15 * 900, 106.0, 112.0, 105.0, 108.0),
        _c(16 * 900, 107.0, 107.0, 98.0, 99.0),
        _c(17 * 900, 97.0, 97.0, 90.0, 91.0),
        _c(18 * 900, 90.0, 92.0, 85.0, 86.0),
    ]
    return cs


class TestFindingTheLiquidityPools:
    def test_two_near_equal_highs_are_one_pool(self):
        cs = _flat(10)
        cs[2] = _c(2 * 900, 100.0, 110.0, 95.0, 100.0)
        cs[6] = _c(6 * 900, 100.0, 110.4, 95.0, 100.0)

        pools = [p for p in ict.detect_equal_levels(cs) if p["type"] == "eq_high"]

        assert pools == [{"price": 110.2, "type": "eq_high", "touches": 2}]

    def test_highs_further_apart_than_the_tolerance_are_not_a_pool(self):
        cs = _flat(10)
        cs[2] = _c(2 * 900, 100.0, 110.0, 95.0, 100.0)
        cs[6] = _c(6 * 900, 100.0, 120.0, 95.0, 100.0)

        assert [p for p in ict.detect_equal_levels(cs) if p["type"] == "eq_high"] == []

    def test_a_handful_of_candles_is_not_enough_to_judge(self):
        assert ict.detect_equal_levels(_flat(4)) == []


class TestTheDefectInEqualLevels:
    """**Known wrong — `docs/todo/bugs/047`.** Pinned so the fix is a visible,
    deliberate change rather than a silent one."""

    def test_exactly_equal_highs_are_invisible(self):
        """Two candles printing the SAME high to 0.1 produce no pool at all,
        because the values are put through `set()` before clustering and
        collapse to one. A textbook double top -- the cleanest equal high
        there is, and the strongest resting-liquidity magnet -- is the one
        shape this detector cannot see."""
        cs = _flat(10)
        cs[2] = _c(2 * 900, 100.0, 110.0, 95.0, 100.0)
        cs[6] = _c(6 * 900, 100.0, 110.0, 95.0, 100.0)

        assert [p for p in ict.detect_equal_levels(cs) if p["type"] == "eq_high"] == []

    def test_moving_one_of_them_by_a_tick_makes_it_appear(self):
        """The same two highs, 0.4 apart instead of 0. This is the pair above
        with one candle nudged, and it is found -- which is what makes the case
        above a defect rather than a threshold."""
        cs = _flat(10)
        cs[2] = _c(2 * 900, 100.0, 110.0, 95.0, 100.0)
        cs[6] = _c(6 * 900, 100.0, 110.4, 95.0, 100.0)

        assert [p for p in ict.detect_equal_levels(cs) if p["type"] == "eq_high"]


class TestTheSweep:
    _POOLS = [{"price": 110.0, "type": "eq_high", "touches": 2}]

    def test_a_wick_through_that_closes_back_is_a_sweep(self):
        cs = _flat(9) + [_c(9 * 900, 108.0, 112.0, 107.0, 109.0)]

        sweep = ict.detect_liquidity_sweep(cs, self._POOLS)

        assert sweep["reversal_direction"] == "bearish"
        assert sweep["swept_at"] == 112.0

    def test_closing_beyond_the_pool_is_a_break_not_a_sweep(self):
        """The whole distinction: taking the stops and coming back is a
        reversal signature; taking them and staying there is a breakout, and
        trading it as a reversal is trading into a trend."""
        cs = _flat(9) + [_c(9 * 900, 108.0, 112.0, 107.0, 111.5)]

        assert ict.detect_liquidity_sweep(cs, self._POOLS) is None

    def test_a_sweep_older_than_the_window_is_not_reported(self):
        """`recent_n` exists so the engine acts on a sweep that just happened,
        not on any historical touch of the same level."""
        cs = ([_c(0, 108.0, 112.0, 107.0, 109.0)] + _flat(9))
        for i, candle in enumerate(cs):
            candle["ts"] = i * 900

        assert ict.detect_liquidity_sweep(cs, self._POOLS, recent_n=5) is None

    def test_a_low_swept_implies_a_bullish_reversal(self):
        cs = _flat(9) + [_c(9 * 900, 96.0, 97.0, 88.0, 95.0)]

        sweep = ict.detect_liquidity_sweep(
            cs, [{"price": 90.0, "type": "eq_low", "touches": 2}])

        assert sweep["reversal_direction"] == "bullish"
        assert sweep["swept_at"] == 88.0

    def test_no_pools_means_nothing_to_sweep(self):
        assert ict.detect_liquidity_sweep(_flat(10), []) is None


class TestTheStructureShift:
    def test_closing_above_the_prior_swing_high_confirms_a_bullish_shift(self):
        cs = _flat(20)
        cs[-1] = _c(19 * 900, 100.0, 112.0, 99.0, 111.0)

        assert ict.detect_market_structure_shift(cs, "bullish") is True

    def test_a_push_that_does_not_clear_the_prior_high_is_not_a_shift(self):
        cs = _flat(20)
        cs[-1] = _c(19 * 900, 100.0, 104.0, 99.0, 103.0)

        assert ict.detect_market_structure_shift(cs, "bullish") is False

    def test_closing_below_the_prior_swing_low_confirms_a_bearish_shift(self):
        cs = _flat(20)
        cs[-1] = _c(19 * 900, 100.0, 101.0, 88.0, 89.0)

        assert ict.detect_market_structure_shift(cs, "bearish") is True

    def test_too_little_history_refuses_even_when_the_shape_is_there(self):
        """Fails CLOSED, unlike the breakout engine's ADX filter which fails
        open. This one is a confirmation, not a veto: no evidence of a shift
        means no shift.

        The last candle here DOES clear every prior high, so this is the case
        that separates "there is not enough history" from "the answer happens
        to be no anyway" -- delete the length guard and this test is the one
        that notices."""
        cs = _flat(10)
        cs[-1] = _c(9 * 900, 100.0, 130.0, 99.0, 129.0)

        assert ict.detect_market_structure_shift(cs, "bullish", lookback=15) is False


class TestTheBreakerBlock:
    def test_it_finds_the_opposing_candle_before_the_impulse(self):
        cs = _flat(10)
        cs[7] = _c(7 * 900, 104.0, 105.0, 96.0, 97.0)
        cs[8] = _c(8 * 900, 98.0, 112.0, 97.0, 111.0)

        assert ict.find_breaker_block(cs, "bullish") == {
            "low": 96.0, "high": 105.0, "idx": 7}

    def test_a_quiet_range_has_no_breaker(self):
        assert ict.find_breaker_block(_flat(10), "bullish") is None

    def test_too_few_candles_to_judge(self):
        assert ict.find_breaker_block(_flat(4), "bullish") is None


class TestTheWholeChain:
    def test_all_four_stages_confirming_produces_an_entry_zone(self):
        setup = ict.find_unicorn_setup(_swept_and_shifted())

        assert setup["direction"] == "bearish"
        assert setup["zone_low"] == 92.0
        assert setup["zone_high"] == 98.0
        assert setup["entry_mid"] == 95.0

    def test_no_sweep_stops_it_at_the_first_stage(self):
        assert ict.find_unicorn_setup(_flat(20)) is None

    def test_a_sweep_with_no_structure_shift_is_refused(self):
        """The stage that separates a real reversal from a stop-hunt that just
        carries on in the old direction. Same sweep as the passing case, with
        the drop that followed it removed."""
        cs = _swept_and_shifted()[:-3] + [
            _c(16 * 900, 108.0, 109.0, 107.0, 108.5),
            _c(17 * 900, 108.0, 109.0, 107.0, 108.5),
            _c(18 * 900, 108.0, 109.0, 107.0, 108.5),
        ]

        assert ict.find_unicorn_setup(cs) is None

    def test_a_zone_far_too_wide_to_be_an_imbalance_is_given_up_on(self):
        """The defence-in-depth guard. A real entry zone off these channels is
        a handful of points wide; a 40-point "zone" means something upstream
        produced a shape that is not an intrabar imbalance, and quoting an
        entry from it would be inventing a price."""
        cs = _swept_and_shifted()
        cs[-2] = _c(17 * 900, 60.0, 60.0, 55.0, 56.0)
        cs[-1] = _c(18 * 900, 55.0, 57.0, 50.0, 51.0)

        assert ict.find_unicorn_setup(cs) is None
