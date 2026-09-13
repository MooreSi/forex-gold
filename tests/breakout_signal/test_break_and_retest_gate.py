"""Break-and-retest: the sequence, and the four things that must all hold.

The other half of the Breakout engine's entry decision, and the half with the
worse history. Its own docstring records why it was rebuilt on 2026-07-16:

> *the old version entered while price was still sitting AT the level with no
> rejection close — catching the knife on every deep pullback that kept going.
> 257 recorded retest trades, 45.9% WR, -$1,657.*

The rebuild added three conditions, and they are a sequence rather than a set:
an **impulsive** prior break, a **first** pullback, and a **rejection close**
confirming the level held. Untested until now.

`adaptive_params.get` reads the database, so it is driven from a fixed table —
the sequence is under test, not the tuning.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import signal_generator as sg

_PARAMS = {
    "min_adx_retest": 20.0,
    "max_adx_entry": 45.0,
    "retest_atr_tolerance": 0.3,
    "lookback_candles": 12.0,
    "min_break_pts": 1.0,
    "min_break_atr_frac": 0.1,
    "require_dual_bias": 0.0,
    "min_level_strength": 1.0,
    "retest_max_prior_touches": 1.0,
}

ATR = 10.0
LEVEL = 4300.0
TOL = 3.0          # retest_atr_tolerance x ATR


@pytest.fixture(autouse=True)
def params(monkeypatch):
    box = dict(_PARAMS)
    monkeypatch.setattr(sg.ap, "get", lambda k: box[k])
    return box


def _c(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _sequence(*, break_body=6.0, touches=0, rejection=True):
    """The canonical BUY retest, built from its three parts.

    Twelve candles: quiet, then an impulsive break above the level, then a
    drift, then a candle that wicks back into the zone and closes above it.
    """
    candles = [_c(4294.0, 4294.5, 4293.5, 4294.0) for _ in range(8)]
    # the impulsive break: body `break_body`, closing above the level
    top = LEVEL + break_body
    candles.append(_c(LEVEL, top + 0.2, LEVEL - 0.2, top))
    # drift candles; each one that dips into the zone is a prior touch
    for i in range(2):
        low = (LEVEL + TOL - 0.5) if i < touches else (LEVEL + TOL + 5.0)
        candles.append(_c(top, top + 0.5, low, top))
    # the rejection candle: wicks into the zone, closes back above, near its high
    if rejection:
        candles.append(_c(LEVEL + 1.0, LEVEL + 4.0, LEVEL + 0.5, LEVEL + 4.0))
    else:
        candles.append(_c(LEVEL + 4.0, LEVEL + 4.2, LEVEL + 0.5, LEVEL + 1.0))
    return candles


def _retest(candles, *, htf="bullish", h4="bullish", adx=30.0,
            price=LEVEL + 2.0, levels=None):
    return sg.check_breakout_retest(
        candles,
        levels if levels is not None else [{"price": LEVEL, "type": "swing_high", "strength": 2}],
        htf, h4, price, ATR, adx, 1.0)


class TestTheSequenceThatQualifies:
    def test_break_then_first_pullback_then_rejection_is_a_candidate(self):
        got = _retest(_sequence())

        assert got is not None
        assert got["direction"] == "BUY"
        assert got["breakout_type"] == "retest"
        assert got["broken_level"] == LEVEL


class TestTheBreakMustHaveBeenImpulsive:
    def test_a_weak_drift_through_the_level_is_not_a_break_worth_retesting(self):
        """Body must be at least half an ATR. A level price wandered across is
        not a level anyone defended."""
        assert _retest(_sequence(break_body=2.0)) is None

    def test_a_body_of_exactly_half_an_atr_qualifies(self):
        assert _retest(_sequence(break_body=5.0)) is not None


class TestFirstPullbackOnly:
    def test_one_prior_touch_is_still_the_first_pullback(self, params):
        params["retest_max_prior_touches"] = 2.0

        assert _retest(_sequence(touches=1)) is not None

    def test_a_touch_already_spent_refuses_the_entry(self):
        """Default is one: each prior dip into the zone consumes the resting
        orders that make the level hold."""
        assert _retest(_sequence(touches=1)) is None


class TestTheRejectionClose:
    def test_no_rejection_candle_means_no_entry(self):
        """The rebuild's whole point — without this the engine entered while
        price sat at the level, and caught every pullback that kept going."""
        assert _retest(_sequence(rejection=False)) is None

    def test_a_candle_that_never_reached_the_zone_is_not_a_retest(self):
        candles = _sequence()
        candles[-1] = _c(LEVEL + 8.0, LEVEL + 9.0, LEVEL + 7.0, LEVEL + 9.0)

        assert _retest(candles) is None

    def test_a_bullish_candle_that_still_closed_below_the_level_is_refused(self):
        """The isolating case. This candle is bullish, closes at its high, and
        wicked into the zone -- it passes every other part of the rejection
        test. It closed BELOW the level, so the level did not hold, and that
        one clause is the whole difference between a retest and a breakdown."""
        candles = _sequence()
        candles[-1] = _c(LEVEL - 6.0, LEVEL - 1.0, LEVEL - 7.0, LEVEL - 1.2)

        assert _retest(candles, price=LEVEL - 1.0) is None

    def test_a_candle_that_closed_back_below_the_level_is_a_failure_not_a_hold(self):
        candles = _sequence()
        candles[-1] = _c(LEVEL + 1.0, LEVEL + 1.2, LEVEL - 5.0, LEVEL - 4.0)

        assert _retest(candles) is None


class TestPriceMustStillBeThere:
    def test_price_far_from_the_level_is_refused(self):
        """The rejection may have happened; if price has already run, the entry
        is no longer at the level the trade is premised on."""
        assert _retest(_sequence(), price=LEVEL + 50.0) is None

    def test_price_just_outside_the_widened_tolerance_is_refused(self):
        assert _retest(_sequence(), price=LEVEL + TOL * 1.5 + 0.1) is None


class TestTheSharedGates:
    def test_below_the_adx_floor_is_refused(self):
        assert _retest(_sequence(), adx=19.9) is None

    def test_above_the_adx_cap_is_refused(self):
        assert _retest(_sequence(), adx=45.1) is None

    def test_a_bearish_higher_timeframe_refuses_a_buy_retest(self):
        assert _retest(_sequence(), htf="bearish") is None

    def test_dual_bias_on_refuses_a_disagreeing_four_hour(self, params):
        params["require_dual_bias"] = 1.0

        assert _retest(_sequence(), h4="bearish") is None

    def test_a_weak_level_is_skipped(self, params):
        params["min_level_strength"] = 3.0

        assert _retest(_sequence()) is None

    def test_under_five_candles_is_not_judged(self):
        assert _retest(_sequence()[:4]) is None
