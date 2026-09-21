"""The confirmation candle -- the thing that turns "price is at my zone" into
"price has reacted at my zone".

Alex G's stop goes below the tail of the confirmation candle, so this module
does not only decide whether to trade: it decides where the stop is. A pin bar
detected on a bar that is really a doji puts the stop a few ticks away and
turns a 1:3 setup into a stop-out.

The negative cases carry the weight here. A detector that says yes to
everything scores every candle as confluence, and the checklist above it would
still read as a high-quality setup.
"""
from __future__ import annotations

from backend.src.services.setforget import patterns

from ._candles import candle


class TestEngulfing:
    def test_a_bullish_engulfing_body_covers_the_previous_body(self):
        prev = candle(0, 100.0, 100.5, 97.0, 97.5)     # down bar, body 97.5-100
        cur = candle(1, 97.2, 101.5, 97.0, 101.0)      # up bar, body 97.2-101

        assert patterns.engulfing(prev, cur) == "bullish"

    def test_a_bearish_engulfing_is_the_mirror(self):
        prev = candle(0, 97.5, 100.5, 97.0, 100.0)     # up bar, body 97.5-100
        cur = candle(1, 100.3, 100.5, 96.0, 96.5)      # down bar, body 96.5-100.3

        assert patterns.engulfing(prev, cur) == "bearish"

    def test_a_bigger_bar_in_the_same_direction_is_not_engulfing(self):
        """Two up bars in a row is momentum, not a reversal signal. The
        previous body has to be the OPPOSITE colour or there is nothing being
        engulfed."""
        prev = candle(0, 97.0, 99.5, 96.5, 99.0)
        cur = candle(1, 99.0, 103.0, 98.0, 102.5)

        assert patterns.engulfing(prev, cur) is None

    def test_a_body_that_does_not_cover_the_whole_previous_body_is_not_engulfing(self):
        prev = candle(0, 100.0, 100.5, 97.0, 97.5)
        cur = candle(1, 97.6, 100.0, 97.4, 99.5)       # closes short of 100.0

        assert patterns.engulfing(prev, cur) is None

    def test_a_doji_never_engulfs_however_wide_its_wicks(self):
        """A bar that opens and closes at the same price has no body to
        engulf with. Its range can span the previous bar entirely and it still
        told you nothing about who won."""
        prev = candle(0, 100.0, 100.5, 97.0, 97.5)
        cur = candle(1, 98.0, 105.0, 90.0, 98.0)

        assert patterns.engulfing(prev, cur) is None


class TestPinBar:
    def test_a_long_lower_tail_with_a_small_body_is_bullish(self):
        # Range 10, body 1, lower tail 8: rejection of the low.
        assert patterns.pin_bar(candle(0, 108.5, 110.0, 100.0, 109.0)) == "bullish"

    def test_a_long_upper_tail_is_bearish(self):
        assert patterns.pin_bar(candle(0, 101.5, 110.0, 100.0, 101.0)) == "bearish"

    def test_a_body_that_fills_the_range_is_not_a_pin_bar(self):
        """A full-bodied trend bar is the opposite of a rejection: nobody
        pushed price back."""
        assert patterns.pin_bar(candle(0, 100.1, 110.0, 100.0, 109.9)) is None

    def test_tails_on_both_sides_are_indecision_not_rejection(self):
        """A small body in the middle of a wide range is a doji. It rejected
        both ends, which means it rejected neither."""
        assert patterns.pin_bar(candle(0, 105.0, 110.0, 100.0, 105.2)) is None

    def test_a_zero_range_bar_is_none_rather_than_a_division_by_zero(self):
        assert patterns.pin_bar(candle(0, 100.0, 100.0, 100.0, 100.0)) is None


class TestConfirmation:
    """What the checklist actually asks: did the last closed bar confirm, and
    which way."""

    def test_reads_the_last_closed_bar_not_the_forming_one(self):
        """The final candle of a live series is still being written. Its close
        is the current price and will move again, so confirming on it is
        confirming on nothing."""
        confirmed = candle(2, 97.2, 101.5, 97.0, 101.0)
        forming = candle(3, 101.0, 101.2, 100.9, 101.1)
        series = [candle(0, 99.0, 99.5, 98.5, 99.0),
                  candle(1, 100.0, 100.5, 97.0, 97.5),
                  confirmed, forming]

        found = patterns.confirmation(series)

        assert found is not None
        assert found["direction"] == "bullish"
        assert found["idx"] == 2
        assert found["low"] == confirmed["low"]

    def test_reports_the_tail_the_stop_goes_beyond(self):
        """Alex G's stop is below the tail of the confirmation candle, so the
        pattern has to hand back the extremes of the bar it fired on. A
        detector that only returns a direction leaves the stop to be guessed."""
        pin = candle(1, 108.5, 110.0, 100.0, 109.0)
        found = patterns.confirmation([candle(0, 108.0, 109.0, 107.0, 108.5), pin,
                                       candle(2, 109.0, 109.5, 108.5, 109.2)])

        assert found is not None
        assert found["kind"] == "pin_bar"
        assert found["low"] == 100.0
        assert found["high"] == 110.0

    def test_no_pattern_is_none(self):
        flat = [candle(i, 100.0, 100.4, 99.6, 100.1) for i in range(4)]
        assert patterns.confirmation(flat) is None

    def test_too_short_a_series_is_none_rather_than_an_error(self):
        assert patterns.confirmation([]) is None
        assert patterns.confirmation([candle(0, 1.0, 2.0, 0.5, 1.5)]) is None


class TestInsideBar:
    """The guide names four patterns; this is the third.

    *"A smaller candle whose range is completely within the previous candle's
    high-low. It signals indecision or a quiet pullback within a trend."*

    Unlike the engulfing and the pin bar it carries NO direction of its own --
    it is a pause, and which way it resolves is the trend's business. So it is
    reported as continuation in the direction already in force, which is what
    makes it usable in a method whose first filter is the higher-timeframe
    bias.
    """

    def test_a_candle_inside_the_previous_range_is_an_inside_bar(self):
        prev = candle(0, 100.0, 110.0, 95.0, 105.0)
        cur = candle(1, 104.0, 108.0, 99.0, 102.0)

        assert patterns.inside_bar(prev, cur) is True

    def test_it_is_the_RANGE_that_must_be_contained_not_the_body(self):
        """Wicks, not bodies -- the guide says "range is completely within the
        previous candle's high-low". A bar whose body is inside but whose wick
        pokes out has broken the prior range, which is the opposite signal."""
        prev = candle(0, 100.0, 110.0, 95.0, 105.0)
        poked = candle(1, 104.0, 112.0, 99.0, 102.0)      # high above prev

        assert patterns.inside_bar(prev, poked) is False

    def test_a_bar_that_breaks_the_low_is_not_inside_either(self):
        prev = candle(0, 100.0, 110.0, 95.0, 105.0)
        broke = candle(1, 104.0, 108.0, 90.0, 102.0)

        assert patterns.inside_bar(prev, broke) is False

    def test_an_identical_range_is_not_an_inside_bar(self):
        """The guide says a "smaller" candle. A bar with the same high and low
        has not contracted -- and without this a run of identical bars, which
        is a dead feed, reports a pattern on every one of them."""
        prev = candle(0, 100.0, 110.0, 95.0, 105.0)
        equal = candle(1, 104.0, 110.0, 95.0, 102.0)

        assert patterns.inside_bar(prev, equal) is False

    def test_touching_one_side_is_still_inside_if_the_range_contracted(self):
        prev = candle(0, 100.0, 110.0, 95.0, 105.0)
        touching = candle(1, 104.0, 110.0, 99.0, 102.0)      # same high, higher low

        assert patterns.inside_bar(prev, touching) is True


class TestDoji:
    """*"A candle where open and close are nearly equal, reflecting market
    indecision. In context, a doji at an AOI can hint at a stall/reversal."*

    "Nearly equal" is the whole difficulty: measured against the bar's own
    range, because a $2 body is a doji on a $40 gold bar and a trend bar on a
    $3 one.
    """

    def test_a_candle_with_almost_no_body_is_a_doji(self):
        assert patterns.doji(candle(0, 100.0, 105.0, 95.0, 100.2)) is True

    def test_a_full_bodied_bar_is_not(self):
        assert patterns.doji(candle(0, 100.0, 110.0, 99.0, 109.0)) is False

    def test_it_is_measured_against_the_bars_own_range(self):
        """The same 0.5 body: indecision on a wide bar, a trend bar on a
        narrow one. A fixed body size would be wrong on every instrument but
        the one it was chosen for."""
        wide = candle(0, 100.0, 110.0, 90.0, 100.5)
        narrow = candle(1, 100.0, 100.6, 99.9, 100.5)

        assert patterns.doji(wide) is True
        assert patterns.doji(narrow) is False

    def test_a_zero_range_bar_is_not_a_doji_rather_than_a_division_by_zero(self):
        """A bar with no range at all is a gap in the feed, not indecision."""
        assert patterns.doji(candle(0, 100.0, 100.0, 100.0, 100.0)) is False


class TestConfirmationPrefersADirectionalPattern:
    def test_an_engulfing_still_wins_over_a_doji(self):
        """The two new patterns are weaker signals and must not displace the
        two that carry a direction. A doji says "something stalled"; an
        engulfing says which way."""
        prev = candle(0, 100.0, 100.5, 97.0, 97.5)
        engulfing = candle(1, 97.2, 101.5, 97.0, 101.0)
        found = patterns.confirmation([candle(0, 99.0, 99.5, 98.5, 99.0), prev,
                                       engulfing, candle(3, 101.0, 101.2, 100.9, 101.1)])

        assert found is not None and found["kind"] == "engulfing"

    def test_a_doji_is_reported_when_there_is_nothing_stronger(self):
        """It is a stall at a level, which is information -- the guide says a
        doji at an AOI "can hint at a stall/reversal"."""
        flat = [candle(0, 100.0, 104.0, 96.0, 100.1),
                candle(1, 100.0, 105.0, 95.0, 100.2),
                candle(2, 100.0, 105.0, 95.0, 100.1),
                candle(3, 100.0, 100.4, 99.6, 100.1)]

        found = patterns.confirmation(flat)

        assert found is not None
        assert found["kind"] == "doji"
        assert found["direction"] is None, "a doji picks no side"
