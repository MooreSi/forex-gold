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

from ._candles import candle, series, zigzag


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


class TestShiftOfStructure:
    """The 30-minute trigger, and the reason this module exists on a third
    timeframe at all.

    Alex G's guide puts the entry on the lowest of three timeframes -- "a 4:1
    or 8:1 ratio (e.g. Daily, 4H, 30-min)" -- and the community's Perfect
    Checklist has a whole group labelled "2H, 1H, 30m" whose heaviest item is
    Shift of Structure. The higher timeframes say WHERE and WHICH WAY; this
    says WHEN.

    A shift is price closing through the last confirmed swing point against the
    move that just happened: for a long, a close above the most recent swing
    high after a run of lower highs into the zone. It is the first evidence
    that the sellers who drove price into the zone have stopped.

    **The close, never the wick.** A wick through a level is a test of it. Only
    a close says the level changed hands, and the guide is explicit about using
    body closes to read structure.
    """

    # Falls, bounces to a swing high at 121, falls to a swing low, then closes
    # back above that 121. The bounce is what creates a level to break -- a
    # series that only falls has no confirmed swing high at all.
    TURNED_UP = [118, 112, 106, 120, 108, 102, 98, 104, 112, 126]
    TURNED_DOWN = [108, 114, 120, 106, 118, 124, 128, 122, 114, 100]

    def test_a_close_above_the_last_swing_high_shifts_a_downmove_up(self):
        candles = series(self.TURNED_UP)

        shift = structure.shift_of_structure(candles, "BUY")

        assert shift is not None
        assert shift["direction"] == "BUY"

    def test_a_close_below_the_last_swing_low_shifts_an_upmove_down(self):
        candles = series(self.TURNED_DOWN)

        shift = structure.shift_of_structure(candles, "SELL")

        assert shift is not None
        assert shift["direction"] == "SELL"

    def test_a_wick_through_the_level_is_not_a_shift(self):
        """A wick through is a test of the level. Only a close says it changed
        hands -- and the guide reads structure from body closes for exactly
        this reason. Without it every spike into a level is an entry."""
        candles = series(self.TURNED_UP[:-1])
        broken = [dict(c) for c in candles]
        # A final bar whose HIGH clears everything but whose close does not.
        broken.append(candle(len(broken) * 3600.0, 107.0, 999.0, 106.0, 108.0))

        assert structure.shift_of_structure(broken, "BUY") is None

    def test_a_move_that_has_not_turned_yet_is_no_shift(self):
        """Still making lower lows into the zone. This is the common case and
        it must answer None, or the trigger fires on arrival at the zone rather
        than on the reaction to it."""
        falling = series([118, 112, 106, 120, 108, 102, 98, 96, 94])

        assert structure.shift_of_structure(falling, "BUY") is None

    def test_it_reports_the_level_that_was_broken(self):
        """The level is what a pending entry would be placed against, so a
        detector that only returned True leaves the price to be guessed."""
        candles = series(self.TURNED_UP)

        shift = structure.shift_of_structure(candles, "BUY")

        assert shift is not None
        assert isinstance(shift["level"], float)
        assert shift["level"] < candles[-1]["close"], "closed through it"

    def test_an_old_shift_does_not_count_as_a_trigger_today(self):
        """A shift twenty bars ago is history. Reading it as a live trigger is
        how an entry fires on a move that already happened and is over."""
        candles = series(self.TURNED_UP + [126 + i for i in range(20)])

        assert structure.shift_of_structure(candles, "BUY", within=3) is None

    def test_the_recency_window_is_the_callers(self):
        candles = series(self.TURNED_UP)

        assert structure.shift_of_structure(candles, "BUY", within=1) is not None

    def test_too_short_a_series_is_none_rather_than_an_error(self):
        assert structure.shift_of_structure([], "BUY") is None
        assert structure.shift_of_structure(series([100, 101]), "BUY") is None
