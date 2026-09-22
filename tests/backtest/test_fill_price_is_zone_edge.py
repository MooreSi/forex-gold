"""The backtest must fill where the live path fills: at the zone edge.

`_simulate` filled every signal at the MIDPOINT of its entry zone. The live
path does not: it fills when price ENTERS the zone, which is the near edge --
and for a signal placed against the move (every reversal signal, and most
Telegram zones) the near edge is the WORST price in the zone for that
direction.

Measured 2026-09-22 against 664 reversal-engine signals that actually reached
the broker and have a recorded `trigger_price`:

    where the real fill landed in the zone, 0.0 = best edge, 1.0 = worst
        p25 0.84   median 0.95   p75 1.00
        98.1% of real fills were in the WORSE half of the zone

    modelled fill minus real fill
        midpoint      $1.34 BETTER than reality  (21% of the stop distance)
        adverse edge  $0.16 worse than reality

A backtest that hands every trade 21% of its stop distance in free entry is
not conservative-by-a-little: on the same 666 executions it scored 135 of 283
real losses as wins, against 18 the other way. It got the SIGN wrong on one
trade in five, almost always in its own favour.

So the rule this file pins: the fill is the edge of the zone that is adverse
to the trade's direction, plus the half-spread. Never the midpoint, and never
anything better than that edge.

`fixed_rr` is used as the strategy throughout because the fill price is
resolved by `_simulate` BEFORE it dispatches to any strategy, and `fixed_rr`
needs no template store and so no database.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import engine as bt

# Bars must sit after the signal in broker time -- `_simulate` skips any bar
# before `created_ts + _BROKER_TZ_OFFSET`, and that offset is 10,800s.
_TS0 = 20_000.0
_SPREAD = 0.4
_HALF = _SPREAD / 2


def _buy_signal() -> bt.BtSignal:
    """A BUY zone of 4000.00-4003.00 -- price falls into it from above."""
    return bt.BtSignal(
        signal_id="buy-1", direction="BUY",
        entry_low=4000.0, entry_high=4003.0,
        stop_loss=3995.0, tp1=4010.0, tp2=None, tp3=None, created_ts=0.0,
    )


def _sell_signal() -> bt.BtSignal:
    """A SELL zone of 4000.00-4003.00 -- price rises into it from below."""
    return bt.BtSignal(
        signal_id="sell-1", direction="SELL",
        entry_low=4000.0, entry_high=4003.0,
        stop_loss=4008.0, tp1=3993.0, tp2=None, tp3=None, created_ts=0.0,
    )


def _bars(*ohlc) -> list[dict]:
    return [{"ts": _TS0 + i * 60, "open": o, "high": h, "low": lo, "close": c}
            for i, (o, h, lo, c) in enumerate(ohlc)]


def _walk(sig, bars):
    return bt._simulate(bars, sig, "fixed_rr", balance=1000.0,
                        risk_pct=1.0, spread_pts=_SPREAD)


def test_buy_falling_into_its_zone_fills_at_the_high_edge():
    # Arrange: price is above the zone and sells off through it, so the first
    # price inside the zone that the buyer can be filled at is its HIGH edge.
    sig = _buy_signal()
    bars = _bars((4006.0, 4006.5, 3999.0, 4001.0),
                 (4001.0, 4004.0, 4000.0, 4002.0))

    # Act
    trade = _walk(sig, bars)

    # Assert
    assert trade is not None
    assert trade.fill_price == pytest.approx(4003.0 + _HALF)


def test_sell_rising_into_its_zone_fills_at_the_low_edge():
    # Arrange: price is below the zone and rallies through it, so the first
    # price inside the zone the seller can be filled at is its LOW edge.
    sig = _sell_signal()
    bars = _bars((3997.0, 4004.0, 3996.5, 4002.0),
                 (4002.0, 4003.5, 4000.5, 4001.0))

    # Act
    trade = _walk(sig, bars)

    # Assert
    assert trade is not None
    assert trade.fill_price == pytest.approx(4000.0 - _HALF)


def test_buy_is_never_filled_better_than_the_adverse_edge():
    # The regression this file exists for. The midpoint fill was 4001.50; the
    # honest one is 4003.00. Asserting the inequality as well as the equality
    # above states the CONTRACT -- a future change may refine where inside the
    # zone the fill lands, but it may never drift back to the cheap side.
    sig = _buy_signal()
    bars = _bars((4006.0, 4006.5, 3999.0, 4001.0))

    trade = _walk(sig, bars)

    assert trade is not None
    assert trade.fill_price >= sig.entry_high
    assert trade.fill_price > sig.entry_mid


def test_sell_is_never_filled_better_than_the_adverse_edge():
    sig = _sell_signal()
    bars = _bars((3997.0, 4004.0, 3996.5, 4002.0))

    trade = _walk(sig, bars)

    assert trade is not None
    assert trade.fill_price <= sig.entry_low
    assert trade.fill_price < sig.entry_mid
