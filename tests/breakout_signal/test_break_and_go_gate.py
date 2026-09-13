"""Every reason `check_breakout_go` says no, and the one path where it says yes.

This is the Breakout engine's primary entry decision — the function that turns
a chart into an order on the engine that has produced 114 signals. It sat
untested: `signal_generator.py` was at 35.7% coverage and everything from line
105 down was uncovered.

Each refusal below was added for a reason the docstrings record, most of them
from live losses. Read together they are a chain, and the chain is the point: a
candidate has to survive ten separate objections, and a test per objection is
what stops one being loosened without anyone noticing.

Nothing here changes behaviour. `adaptive_params.get` reads the database, so
it is driven from a fixed table — the arithmetic is under test, not the tuning.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import signal_generator as sg

_PARAMS = {
    "min_adx_go": 25.0,
    "max_adx_entry": 45.0,
    "min_break_pts": 1.0,
    "require_dual_bias": 0.0,
    "min_break_atr_frac": 0.1,
    "exhaustion_atr_mult": 1.5,
    "min_level_strength": 1.0,
    "require_adx_rising": 0.0,
    "compression_max_range_atr": 10.0,
}

ATR = 10.0
LEVEL = 4300.0


@pytest.fixture(autouse=True)
def params(monkeypatch):
    box = dict(_PARAMS)
    monkeypatch.setattr(sg.ap, "get", lambda k: box[k])
    return box


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _quiet(n=20, price=4295.0):
    """A compressed run below the level: nothing breaking anything."""
    return [_c(price, price + 0.5, price - 0.5, price) for _ in range(n)]


def _buy_break():
    """A clean break-and-go above LEVEL, passing every gate.

    The last candle opens below the level and closes 5 points above it, with a
    body of 5 (>= 35% of ATR), closing at its high (no rejection wick).
    """
    candles = _quiet()
    candles[-1] = _c(4300.0, 4305.0, 4299.8, 4305.0)
    return candles


def _go(candles, *, htf="bullish", h4="bullish", adx=30.0, macd=1.0,
        levels=None, atr=ATR):
    return sg.check_breakout_go(
        candles, levels if levels is not None else [{"price": LEVEL, "type": "swing_high", "strength": 2}],
        htf, h4, LEVEL + 5, atr, adx, macd)


class TestTheHappyPath:
    def test_a_clean_break_returns_a_candidate(self):
        got = _go(_buy_break())

        assert got is not None
        assert got["direction"] == "BUY"
        assert got["breakout_type"] == "go"
        assert got["broken_level"] == LEVEL

    def test_it_reports_how_far_beyond_the_level_the_body_closed(self):
        got = _go(_buy_break())

        assert got["pts_beyond"] == pytest.approx(5.0)

    def test_it_carries_the_level_type_and_strength_through(self):
        """Both feed the level filter and the learning, so a candidate that
        forgot which level it broke is a candidate that cannot be scored."""
        got = _go(_buy_break())

        assert got["broken_level_type"] == "swing_high"
        assert got["level_strength"] == 2


class TestTheAdxWindow:
    def test_too_little_trend_is_refused(self):
        assert _go(_buy_break(), adx=24.9) is None

    def test_too_much_trend_is_refused(self):
        """A cap as well as a floor: the break is wanted early in a leg, not
        after the move has already run."""
        assert _go(_buy_break(), adx=45.1) is None

    def test_the_floor_itself_is_allowed(self):
        assert _go(_buy_break(), adx=25.0) is not None

    def test_the_cap_itself_is_allowed(self):
        assert _go(_buy_break(), adx=45.0) is not None


class TestTheBiasGates:
    def test_a_buy_break_against_a_bearish_higher_timeframe_is_refused(self):
        assert _go(_buy_break(), htf="bearish") is None

    def test_a_neutral_higher_timeframe_still_allows_it(self):
        """Neutral is not an objection. Only a decided bias the other way is."""
        assert _go(_buy_break(), htf="neutral") is not None

    def test_dual_bias_off_ignores_the_four_hour(self, params):
        params["require_dual_bias"] = 0.0

        assert _go(_buy_break(), htf="bullish", h4="bearish") is not None

    def test_dual_bias_on_refuses_a_disagreeing_four_hour(self, params):
        params["require_dual_bias"] = 1.0

        assert _go(_buy_break(), htf="bullish", h4="bearish") is None


class TestTheCandleItself:
    def test_a_candle_far_bigger_than_atr_is_a_spike_not_a_break(self):
        """The anti-exhaustion guard: a climactic stop-hunt spikes through a
        level and mean-reverts, and those are the worst entries this engine
        takes."""
        candles = _quiet()
        candles[-1] = _c(4280.0, 4306.0, 4279.0, 4306.0)   # body 26 > 1.5 x ATR

        assert _go(candles) is None

    def test_a_body_under_a_third_of_atr_is_too_slight(self):
        candles = _quiet(price=4299.0)
        candles[-1] = _c(4299.0, 4301.5, 4298.9, 4301.5)   # body 2.5 < 0.35 x ATR

        assert _go(candles) is None

    def test_a_long_rejection_wick_is_a_failed_break(self):
        """Closing back in the lower two thirds of its own range means price
        was pushed back through the level before the candle ended."""
        candles = _quiet()
        candles[-1] = _c(4300.0, 4312.0, 4299.8, 4305.0)   # closes mid-range

        assert _go(candles) is None


class TestTheBreakMustBeFresh:
    def test_a_level_already_broken_last_candle_is_not_a_new_break(self):
        """The candle before must have been at or below the level. Otherwise
        this is the middle of a move, not its start."""
        candles = _quiet()
        candles[-2] = _c(4304.0, 4304.5, 4303.5, 4304.0)   # already above
        candles[-1] = _c(4304.0, 4309.0, 4303.8, 4309.0)

        assert _go(candles) is None

    def test_a_body_that_does_not_clear_the_minimum_is_refused(self, params):
        """The floor is the greater of the configured points and a fraction of
        ATR, so it scales with how much the instrument is actually moving."""
        params["min_break_atr_frac"] = 1.0   # floor becomes 10 points

        assert _go(_buy_break()) is None


class TestTheLevelFilter:
    def test_a_level_below_the_strength_floor_is_skipped(self, params):
        params["min_level_strength"] = 3.0

        assert _go(_buy_break()) is None

    def test_a_level_with_no_price_is_skipped(self):
        assert _go(_buy_break(), levels=[{"price": 0, "type": "x", "strength": 5}]) is None

    def test_no_levels_at_all_means_no_candidate(self):
        assert _go(_buy_break(), levels=[]) is None


class TestMomentum:
    def test_a_buy_break_needs_positive_macd(self):
        assert _go(_buy_break(), macd=-0.1) is None

    def test_a_flat_macd_is_not_positive(self):
        """`> 0`, not `>= 0`. A break with no momentum behind it is the one
        this gate exists to refuse."""
        assert _go(_buy_break(), macd=0.0) is None


class TestTheSellMirror:
    def _sell_break(self):
        candles = _quiet(price=4305.0)
        candles[-1] = _c(4300.0, 4300.2, 4295.0, 4295.0)
        return candles

    def test_a_clean_break_below_support_returns_a_sell(self):
        got = sg.check_breakout_go(
            self._sell_break(), [{"price": LEVEL, "type": "swing_low", "strength": 2}],
            "bearish", "bearish", LEVEL - 5, ATR, 30.0, -1.0)

        assert got is not None
        assert got["direction"] == "SELL"
        assert got["pts_beyond"] == pytest.approx(5.0)

    def test_a_sell_break_needs_negative_macd(self):
        got = sg.check_breakout_go(
            self._sell_break(), [{"price": LEVEL, "type": "swing_low", "strength": 2}],
            "bearish", "bearish", LEVEL - 5, ATR, 30.0, 0.1)

        assert got is None


class TestNotEnoughHistory:
    def test_under_fifteen_candles_is_not_judged(self):
        """Compression is measured over a window; without it there is nothing
        to say the break escaped from."""
        assert _go(_quiet(n=14)) is None
