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
