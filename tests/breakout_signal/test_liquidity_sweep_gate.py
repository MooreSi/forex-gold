"""The sweep: the failed break turned from a trap into the entry.

The third of the Breakout engine's entry paths, and the inverse of the other
two. Its docstring says what it is for:

> *the mirror image of the failed break-and-go: the spike through the level
> that mean-reverts — the exact pattern that produced most breakout losses in
> the 12:00-15:00 UTC window — becomes the entry instead of the trap.*

So its gates run the other way round, and that is what these tests are mostly
about. **ADX is capped, not floored** — a strong trend keeps running through
swept levels instead of reversing, so the one thing the other two paths require
is the one thing this path refuses. Getting that backwards would turn the
reversal entry into a trend-continuation entry at the worst possible price.

Untested until now. `adaptive_params.get` reads the database, so it is driven
from a fixed table — the shape is under test, not the tuning.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import signal_generator as sg

_PARAMS = {
    "max_adx_sweep": 35.0,
    "min_level_strength": 1.0,
    "require_dual_bias": 0.0,
    "min_break_pts": 1.0,
    "sweep_wick_atr_frac": 0.2,
}

ATR = 10.0
LEVEL = 4300.0
MIN_PIERCE = 2.0      # max(1.0, ATR x 0.2)
CLOSE_BUF = 0.5       # ATR x 0.05


@pytest.fixture(autouse=True)
def params(monkeypatch):
    box = dict(_PARAMS)
    monkeypatch.setattr(sg.ap, "get", lambda k: box[k])
    return box


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _sell_sweep(*, pierce=4.0, close=LEVEL - 3.0, open_=LEVEL - 1.0):
    """A wick through resistance that closes back below it."""
    return [_c(LEVEL - 2, LEVEL - 1, LEVEL - 3, LEVEL - 2),
            _c(LEVEL - 2, LEVEL - 1, LEVEL - 3, LEVEL - 2),
            _c(open_, LEVEL + pierce, LEVEL - 4.0, close)]


def _buy_sweep(*, pierce=4.0, close=LEVEL + 3.0, open_=LEVEL + 1.0):
    return [_c(LEVEL + 2, LEVEL + 3, LEVEL + 1, LEVEL + 2),
            _c(LEVEL + 2, LEVEL + 3, LEVEL + 1, LEVEL + 2),
            _c(open_, LEVEL + 4.0, LEVEL - pierce, close)]


def _sweep(candles, *, htf="bearish", h4="bearish", adx=20.0,
           price=LEVEL + 1.0, levels=None):
    return sg.check_liquidity_sweep(
        candles,
        levels if levels is not None else [{"price": LEVEL, "type": "swing_high", "strength": 2}],
        htf, h4, price, ATR, adx, 0.0)


class TestTheSellSweep:
    def test_a_wick_through_resistance_that_closes_back_below_is_a_sell(self):
        got = _sweep(_sell_sweep())

        assert got is not None
        assert got["direction"] == "SELL"
        assert got["breakout_type"] == "sweep"

    def test_it_reports_the_wick_extreme_for_the_stop(self):
        """The stop goes beyond the sweep wick, so the candidate has to carry
        where that was — not the level, and not the close."""
        got = _sweep(_sell_sweep(pierce=6.0))

        assert got["wick_extreme"] == pytest.approx(LEVEL + 6.0)
        assert got["pierce_pts"] == pytest.approx(6.0)


class TestAdxIsCappedNotFloored:
    """The inversion that matters. A sweep is a reversal play, and a very
    strong trend keeps running through swept levels rather than turning."""

    def test_a_quiet_market_is_allowed(self):
        assert _sweep(_sell_sweep(), adx=5.0) is not None

    def test_a_strong_trend_is_refused(self):
        assert _sweep(_sell_sweep(), adx=35.1) is None

    def test_the_cap_itself_is_allowed(self):
        assert _sweep(_sell_sweep(), adx=35.0) is not None


class TestThePierceMustBeReal:
    def test_a_wick_that_barely_grazed_the_level_is_not_a_sweep(self):
        """Below the floor there is no liquidity taken, and no reason for the
        level to hold any better afterwards."""
        assert _sweep(_sell_sweep(pierce=1.9)) is None

    def test_the_floor_itself_qualifies(self):
        assert _sweep(_sell_sweep(pierce=2.0)) is not None

    def test_the_floor_scales_with_volatility(self, params):
        """A fixed point threshold is meaningless across gold's ATR range, so
        the floor is the greater of the configured points and a fraction of
        ATR."""
        params["sweep_wick_atr_frac"] = 1.0     # floor becomes 10 points

        assert _sweep(_sell_sweep(pierce=4.0)) is None


class TestTheCloseMustRejectIt:
    def test_a_close_still_above_the_level_is_a_break_not_a_sweep(self):
        assert _sweep(_sell_sweep(close=LEVEL + 1.0)) is None

    def test_a_close_only_just_back_inside_does_not_count(self):
        """There is a margin — 5% of ATR — so a close sitting on the level is
        not read as a rejection of it.

        The open is above the close on purpose: with a lower open this candle
        is bullish and `c < o` refuses it instead, which would leave the margin
        itself untested."""
        assert _sweep(_sell_sweep(open_=LEVEL + 0.5,
                                  close=LEVEL - CLOSE_BUF + 0.1)) is None

    def test_a_close_clearly_back_inside_does(self):
        """Just past the margin. The open has to sit above that close or the
        candle is bullish and a different clause refuses it -- which is the
        trap this pair of tests exists to keep apart."""
        assert _sweep(_sell_sweep(open_=LEVEL + 0.5,
                                  close=LEVEL - CLOSE_BUF - 0.1)) is not None

    def test_the_rejection_candle_must_be_bearish(self):
        """Wicked above, closed below the level, but closed UP on the candle:
        that is buyers stepping in, not sellers rejecting."""
        assert _sweep(_sell_sweep(open_=LEVEL - 4.0, close=LEVEL - 3.0)) is None


class TestPriceMustStillBeAtTheLevel:
    def test_price_that_has_already_run_is_refused(self):
        assert _sweep(_sell_sweep(), price=LEVEL + ATR * 0.5 + 0.1) is None

    def test_price_inside_the_window_is_allowed(self):
        assert _sweep(_sell_sweep(), price=LEVEL + ATR * 0.5) is not None


class TestTheBuyMirror:
    def test_a_wick_below_support_that_closes_back_above_is_a_buy(self):
        got = _sweep(_buy_sweep(), htf="bullish", h4="bullish",
                     price=LEVEL - 1.0,
                     levels=[{"price": LEVEL, "type": "swing_low", "strength": 2}])

        assert got is not None
        assert got["direction"] == "BUY"
        assert got["wick_extreme"] == pytest.approx(LEVEL - 4.0)

    def test_a_buy_sweep_needs_a_bullish_rejection_candle(self):
        got = _sweep(_buy_sweep(open_=LEVEL + 4.0, close=LEVEL + 3.0),
                     htf="bullish", h4="bullish", price=LEVEL - 1.0,
                     levels=[{"price": LEVEL, "type": "swing_low", "strength": 2}])

        assert got is None


class TestTheSharedGates:
    def test_a_bullish_bias_refuses_a_sell_sweep(self):
        assert _sweep(_sell_sweep(), htf="bullish") is None

    def test_dual_bias_on_refuses_a_disagreeing_four_hour(self, params):
        params["require_dual_bias"] = 1.0

        assert _sweep(_sell_sweep(), h4="bullish") is None

    def test_a_weak_level_is_skipped(self, params):
        params["min_level_strength"] = 3.0

        assert _sweep(_sell_sweep()) is None

    def test_under_three_candles_is_not_judged(self):
        assert _sweep(_sell_sweep()[:2]) is None

    def test_no_atr_means_no_judgement(self):
        """Every threshold here is ATR-scaled: with a zero ATR the close margin
        collapses to nothing and the "still near the level" window closes to a
        single price.

        Price is exactly at the level so that window is satisfiable — otherwise
        the call is refused for the wrong reason and this says nothing about
        the ATR guard at all."""
        assert sg.check_liquidity_sweep(
            _sell_sweep(), [{"price": LEVEL, "type": "swing_high", "strength": 2}],
            "bearish", "bearish", LEVEL, 0.0, 20.0, 0.0) is None

    def test_a_candle_missing_a_price_is_not_judged(self):
        candles = _sell_sweep()
        candles[-1] = _c(0, LEVEL + 4.0, LEVEL - 4.0, LEVEL - 3.0)

        assert _sweep(candles) is None
