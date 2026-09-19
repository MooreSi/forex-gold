"""Market structure: swing points, and the bias read off them.

This is the bottom of the Set & Forget stack. Everything above it -- the areas
of interest, the confluence score, the setup the AI is asked to judge -- is
derived from the swing points this module finds, so an error here is invisible
at every layer above and wrong in all of them.

Two properties are worth more than the rest:

1. **A swing is only a swing once it is confirmed.** The last bar of a rising
   series is the highest bar in that series, and it is not a swing high: there
   are no bars after it to turn back down. Counting it would let a trade be
   taken against a "structure" that is just the right-hand edge of the chart.
2. **Bias needs two of each.** One higher high does not make an uptrend. It
   takes a higher high AND a higher low, which is the whole reason the bias
   function reads four points and not two.
"""
from __future__ import annotations

from backend.src.services.setforget import structure

from ._candles import series, zigzag


def _prices(points, kind):
    return [round(p["price"], 4) for p in points if p["kind"] == kind]


class TestSwingPoints:
    def test_finds_the_turning_point_of_a_single_peak(self):
        # Up for five bars, down for five. The peak is the sixth close.
        candles = zigzag([(100.0, 0), (110.0, 5), (100.0, 5)])
        highs = [p for p in structure.swing_points(candles) if p["kind"] == "high"]

        assert len(highs) == 1
        assert highs[0]["idx"] == 5
        assert highs[0]["price"] == candles[5]["high"]

    def test_the_final_bar_is_never_a_swing(self):
        """A series that only rises has no confirmed swing high at all.

        The highest bar is the last one, and nothing has turned back down from
        it yet. Reporting it would be reporting the edge of the chart.
        """
        candles = zigzag([(100.0, 0), (130.0, 20)])

        assert structure.swing_points(candles) == []

    def test_lookback_decides_how_much_confirmation_a_swing_needs(self):
        """A small peak is a swing at lookback=1 and noise at lookback=3.

        Bar 4 is the highest of its immediate neighbours, and it is dwarfed by
        bar 7 three bars later. Which of those facts wins is the entire job of
        the lookback, and it is why the timeframes do not share one.
        """
        candles = series([100, 99, 98, 101, 105, 103, 104, 110, 109, 108, 100, 99, 98])

        assert 4 in [p["idx"] for p in structure.swing_points(candles, lookback=1)]
        assert 4 not in [p["idx"] for p in structure.swing_points(candles, lookback=3)]

    def test_points_come_back_in_time_order(self):
        candles = zigzag([(100.0, 0), (120.0, 6), (105.0, 6), (130.0, 6), (115.0, 6)])
        idxs = [p["idx"] for p in structure.swing_points(candles)]

        assert idxs == sorted(idxs)

    def test_a_point_carries_the_timestamp_of_its_own_candle(self):
        """The browser plots against time, so a swing that cannot say WHEN it
        happened cannot be drawn on the chart beside the price it describes."""
        candles = zigzag([(100.0, 0), (110.0, 5), (100.0, 5)])
        point = structure.swing_points(candles)[0]

        assert point["ts"] == candles[point["idx"]]["ts"]

    def test_too_short_a_series_is_no_points_rather_than_an_error(self):
        assert structure.swing_points(series([100, 101])) == []
        assert structure.swing_points([]) == []


class TestBias:
    def test_higher_highs_and_higher_lows_is_bullish(self):
        candles = zigzag([
            (100.0, 0), (120.0, 6), (108.0, 6), (135.0, 6), (122.0, 6), (140.0, 6),
        ])
        assert structure.bias(candles) == "bullish"

    def test_lower_lows_and_lower_highs_is_bearish(self):
        candles = zigzag([
            (140.0, 0), (120.0, 6), (132.0, 6), (105.0, 6), (118.0, 6), (100.0, 6),
        ])
        assert structure.bias(candles) == "bearish"

    def test_a_higher_high_with_a_lower_low_is_ranging(self):
        """An expanding range is not a trend. Both sides moved, and the whole
        point of reading structure is to refuse a pair that is doing this."""
        candles = zigzag([
            (100.0, 0), (120.0, 6), (108.0, 6), (130.0, 6), (95.0, 6), (125.0, 6),
        ])
        assert structure.bias(candles) == "ranging"

    def test_not_enough_confirmed_swings_is_unknown_not_a_guess(self):
        """"unknown" and "ranging" are different answers. Ranging means the
        structure was read and it disagrees with itself; unknown means there
        was not enough chart to read. A setup may not be built on either, and
        the page says which one it hit."""
        assert structure.bias(zigzag([(100.0, 0), (130.0, 20)])) == "unknown"
        assert structure.bias([]) == "unknown"


class TestLastImpulse:
    """The most recent completed leg -- what a Fibonacci retracement is drawn
    over. Swing low to the swing high after it in an uptrend, and the mirror."""

    def test_an_uptrend_impulse_runs_low_to_high(self):
        candles = zigzag([(100.0, 0), (120.0, 6), (108.0, 6), (140.0, 6), (128.0, 6)])
        leg = structure.last_impulse(candles, "bullish")

        assert leg is not None
        assert leg["start"] < leg["end"]
        assert round(leg["end"], 4) == round(candles[18]["high"], 4)

    def test_a_downtrend_impulse_runs_high_to_low(self):
        candles = zigzag([(140.0, 0), (120.0, 6), (132.0, 6), (100.0, 6), (112.0, 6)])
        leg = structure.last_impulse(candles, "bearish")

        assert leg is not None
        assert leg["start"] > leg["end"]

    def test_no_completed_leg_is_none(self):
        assert structure.last_impulse(zigzag([(100.0, 0), (130.0, 20)]), "bullish") is None


class TestAnImpulseWithNothingBeforeIt:
    def test_a_swing_with_no_earlier_opposite_swing_is_no_leg(self):
        """The chart opens mid-move: the first confirmed swing is a high, and
        there is no low before it to measure the leg from. Reporting a leg
        anyway would measure the retracement against the left edge of the
        window, which moves every time the window does."""
        # Falls, then rises: the first confirmed point is a LOW, so a bearish
        # read (which wants a low with a high before it) has no leg.
        candles = zigzag([(140.0, 0), (100.0, 8), (150.0, 8)])

        assert structure.last_impulse(candles, "bearish") is None
        assert structure.last_impulse(candles, "bullish") is None
