"""Per-strategy backtest simulators.

These decide the numbers on the Backtest page, which is where a strategy gets
judged before it is trusted with money. Nothing here places a trade -- but a
simulator that flatters a strategy is how a bad one gets chosen, so the
arithmetic matters as much as any live path.

The file was at 0% coverage. What is pinned below is the shared shape every
simulator has to get right:

  * a stop is a stop -- the SL branch must fire on the correct side for the
    direction, or a losing trade is scored as a winner;
  * a banked partial stays banked whatever happens afterwards;
  * a trade that runs out of bars is priced at the MARKET, not at entry.

That last one was wrong in seven of the eight simulators when these tests were
written -- see docs/todo/bugs/017.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import simulators as sim
from backend.src.services.backtest.engine import (
    BtSignal, _MAX_HOLD_BARS, _USD_PER_PT_PER_LOT,
)


def _sig(direction="BUY", **over):
    base = dict(signal_id="S1", direction=direction,
                entry_low=4000.0, entry_high=4002.0, stop_loss=3990.0,
                tp1=4010.0, tp2=4020.0, tp3=4030.0, created_ts=0.0)
    base.update(over)
    return BtSignal(**base)


def _flat(n, price=4000.0):
    """n candles that never move -- nothing triggers, so a simulator can only
    reach its timeout branch."""
    return [{"open": price, "high": price, "low": price, "close": price,
             "time": i * 300} for i in range(n)]


def _drift(n, start=4000.0, step=-0.05):
    """A slow drift with no wick wide enough to hit a 5-10pt stop or target:
    the trade is still open when the bars run out."""
    out = []
    px = start
    for i in range(n):
        out.append({"open": px, "high": px + 0.01, "low": px - 0.01,
                    "close": px, "time": i * 300})
        px = round(px + step, 4)
    return out


# Two argument shapes: some take the signal's own stop distance, some derive
# their own. Wrapped so the shared tests below can drive them identically.
def _plain(fn):
    def _call(candles, sig, is_buy):
        return fn(candles, sig, 0, 4000.0, is_buy, 1000.0, 1.0)
    return _call


def _with_sl_dist(fn):
    def _call(candles, sig, is_buy):
        return fn(candles, sig, 0, 4000.0, is_buy, 10.0, 1000.0, 1.0)
    return _call


ALL_SIMULATORS = [
    ("conservative",     _plain(sim._simulate_conservative)),
    ("ct",               _plain(sim._simulate_ct)),
    ("reversal_runner",  _plain(sim._simulate_reversal_runner)),
    ("signal_climber",   _plain(sim._simulate_signal_climber)),
    ("adaptive_runner",  _plain(sim._simulate_adaptive_runner)),
    ("nss",              _with_sl_dist(sim._simulate_nss)),
    ("be_runner",        _with_sl_dist(sim._simulate_be_runner)),
    ("scale_out",        _with_sl_dist(sim._simulate_scale_out)),
    ("protected_scale",  _with_sl_dist(sim._simulate_protected_scale)),
    ("trail_stop",       _with_sl_dist(sim._simulate_trail_stop)),
]


class TestATimeoutIsPricedAtTheMarket:
    """bugs/017. A trade still open when the bars run out is closed at the
    last candle, exactly as _run_ladder_strategy already did. Pricing it at
    the fill instead reports every timed-out trade as break-even, which
    flatters any strategy whose trades tend to run long and drift."""

    @pytest.mark.parametrize("name,fn", ALL_SIMULATORS)
    def test_a_buy_that_drifted_down_books_the_loss(self, name, fn):
        candles = _drift(_MAX_HOLD_BARS + 5, start=4000.0, step=-0.05)
        trade = fn(candles, _sig("BUY"), True)

        assert trade.outcome == "timeout", f"{name} did not time out"
        # Asserted against the bar the trade says it closed on, not a fixed
        # index: the GDVR-family simulators run to a different max-hold than
        # the rest, and hard-coding one limit would only be testing that.
        assert trade.close_price == pytest.approx(
            candles[trade.close_bar_idx]["close"]), (
            f"{name} priced the timeout at the fill, not the market")
        assert trade.pnl_pts < 0, f"{name} scored a losing drift as break-even"

    @pytest.mark.parametrize("name,fn", ALL_SIMULATORS)
    def test_a_sell_that_drifted_up_books_the_loss(self, name, fn):
        """The direction must be respected, or a loss is booked as a gain."""
        candles = _drift(_MAX_HOLD_BARS + 5, start=4000.0, step=+0.05)
        trade = fn(candles, _sig("SELL", stop_loss=4010.0,
                                 tp1=3990.0, tp2=3980.0, tp3=3970.0), False)

        assert trade.outcome == "timeout", f"{name} did not time out"
        assert trade.pnl_pts < 0, f"{name} scored a losing drift as break-even"

    @pytest.mark.parametrize("name,fn", ALL_SIMULATORS)
    def test_a_flat_market_still_books_break_even(self, name, fn):
        """The case the old code got right by accident: price never moved, so
        break-even is the correct answer, not just the default one."""
        candles = _flat(_MAX_HOLD_BARS + 5)
        trade = fn(candles, _sig("BUY"), True)

        assert trade.outcome == "timeout"
        assert trade.pnl_pts == pytest.approx(0.0)

    @pytest.mark.parametrize("name,fn", ALL_SIMULATORS)
    def test_the_usd_figure_agrees_with_the_points(self, name, fn):
        """pnl_usd and pnl_pts are read side by side on the Backtest page. A
        points column showing a loss beside a zero P&L column is how this bug
        would have been spotted, if anything had been asserting it."""
        candles = _drift(_MAX_HOLD_BARS + 5, start=4000.0, step=-0.05)
        trade = fn(candles, _sig("BUY"), True)

        assert trade.pnl_usd < 0, f"{name} reported points lost but no money lost"


class TestTheLadderWasAlreadyRight:
    """_run_ladder_strategy priced its timeout at the last close from the
    start. It is the reference the other seven now match."""

    def test_it_prices_a_timeout_at_the_last_close(self):
        candles = _drift(_MAX_HOLD_BARS + 5, start=4000.0, step=-0.05)
        trade = sim._run_ladder_strategy(
            candles, _sig("BUY"), 0, 4000.0, True, 10.0, 0.10,
            "scale_out", {3: [0.5, 0.5]}, 0, _MAX_HOLD_BARS)

        assert trade.outcome == "timeout"
        assert trade.close_price == pytest.approx(
            candles[trade.close_bar_idx]["close"])
        assert trade.pnl_pts < 0


class TestStopsFireOnTheRightSide:
    def test_a_buy_stops_out_when_the_low_reaches_the_stop(self):
        candles = _flat(10)
        candles[3] = {"open": 4000.0, "high": 4000.0, "low": 3990.0,
                      "close": 3995.0, "time": 900}

        trade = sim._simulate_conservative(
            candles, _sig("BUY"), 0, 4000.0, True, 1000.0, 1.0)

        assert trade.outcome == "sl"
        assert trade.pnl_pts < 0

    def test_a_sell_stops_out_when_the_high_reaches_the_stop(self):
        """The mirror. Using the low for a sell would never stop out at all."""
        candles = _flat(10)
        candles[3] = {"open": 4000.0, "high": 4010.0, "low": 4000.0,
                      "close": 4005.0, "time": 900}

        trade = sim._simulate_conservative(
            candles, _sig("SELL"), 0, 4000.0, False, 1000.0, 1.0)

        assert trade.outcome == "sl"
        assert trade.pnl_pts < 0


class TestABankedPartialStaysBanked:
    def test_a_conservative_tp1_partial_survives_a_later_stop_out(self):
        """80% is closed at TP1 and the stop moves to break-even. If the rest
        stops out, the banked 80% must still be in the total -- dropping it
        turns a small win into a scratch."""
        candles = _flat(20)
        candles[2] = {"open": 4000.0, "high": 4003.0, "low": 4000.0,
                      "close": 4003.0, "time": 600}
        candles[6] = {"open": 4000.0, "high": 4000.0, "low": 3999.0,
                      "close": 3999.0, "time": 1800}

        trade = sim._simulate_conservative(
            candles, _sig("BUY"), 0, 4000.0, True, 1000.0, 1.0)

        assert trade.outcome == "tp1_only"
        assert trade.pnl_usd > 0, "the banked TP1 partial was lost"
