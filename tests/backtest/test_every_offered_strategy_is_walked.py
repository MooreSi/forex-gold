"""A strategy the picker offers must be walked, or say why not.

Found live on 2026-09-19 against the demo account's own 480 recorded signals:
`fixed_rr` -- the FIRST option in the picker and the baseline every other row
is compared against -- returned **0 trades, $0.00, and no reason**, while
`scale_out` returned 305 trades from the same signals. Five more did the same:
`scalp_runner`, `gold_diggers_copy`, `orb_fixed`, `adaptive_runner_2` and
`limit_runner`.

The cause was a missing `elif` branch. `_simulate`'s dispatch chain ends in
`else: return None`, so a strategy with no branch produces no trade for any
signal, every time, silently.

**Zeros are not a neutral answer here.** The comparison table puts them beside
a strategy showing a real drawdown, where "0 trades, 0 loss" reads as the safer
choice -- an argument FOR the strategy that was never tested at all. The
template walk already understood this; `unsupported_reason` exists for exactly
this reason, and `run_backtest`'s own docstring says a refused template must
never come back as zeros. Built-in strategies were simply never held to it.

So this file holds the whole catalogue to one rule:

    every strategy the picker offers either produces trades on signals that
    fill, or comes back carrying a reason.

It is deliberately a property of the CATALOGUE rather than a list of names. A
strategy added to the picker tomorrow is covered the day it is added, which a
hand-written list would not be.
"""
from __future__ import annotations

import pytest

from backend.src.controllers import trading_controller as trading_ctl
from backend.src.services.backtest import engine as bt

# Bars sit AFTER the signal in broker time: `_simulate` skips any bar before
# `created_ts + _BROKER_TZ_OFFSET`, and that offset is 10,800s.
_TS0 = 20_000.0


def _signal(**over) -> bt.BtSignal:
    """A BUY with a zone, a stop below it and three targets above.

    Ordinary in every way a strategy could care about: it has a real entry
    zone (not a point), a stop on the correct side, and enough TPs that a
    ladder has something to climb.
    """
    fields = dict(
        signal_id="s1", direction="BUY",
        entry_low=3999.0, entry_high=4001.0, stop_loss=3990.0,
        tp1=4010.0, tp2=4020.0, tp3=4030.0, created_ts=0.0,
    )
    fields.update(over)
    return bt.BtSignal(**fields)


def _rising_candles() -> list[dict]:
    """Fills on bar 0, then walks up through every TP and stays there.

    A series that resolves: any strategy that fills at all reaches an outcome
    rather than timing out, so "no trade" cannot be blamed on the data.
    """
    bars = [(4001.0, 3999.0)]
    price = 4001.0
    for _ in range(60):
        price += 1.0
        bars.append((price + 0.5, price - 0.5))
    return [{"ts": _TS0 + i * 60, "high": hi, "low": lo, "open": lo, "close": hi}
            for i, (hi, lo) in enumerate(bars)]


def _builtin_keys() -> list[str]:
    """Every non-template strategy the Backtest picker offers.

    Templates are excluded because they already carry their own refusal
    machinery, tested in test_unsupported_template_reason.py, and because they
    depend on what is saved in the database.
    """
    return [
        s["key"] for s in trading_ctl.build_strategy_catalogue()
        if not str(s["key"]).startswith(bt.TEMPLATE_PREFIX)
    ]


def _walk(strategy: str):
    return bt.run_backtest(
        [_signal()], _rising_candles(), [strategy],
        starting_balance=1000.0, risk_pct=1.0, spread_pts=0.4,
        commission_per_lot=0.0,
    )[strategy]


class TestTheCatalogueIsWalkable:

    def test_the_picker_offers_something(self):
        # A guard on the guard: an empty catalogue would make every
        # parametrised test below vacuously pass.
        assert len(_builtin_keys()) >= 5

    @pytest.mark.parametrize("strategy", _builtin_keys())
    def test_it_either_trades_or_says_why_not(self, strategy):
        stats = _walk(strategy)

        assert stats.trades > 0 or stats.unsupported_reason, (
            f"{strategy!r} produced no trades and gave no reason. In the "
            f"comparison table that is '0 trades, no loss' beside a strategy "
            f"with a real drawdown, which reads as the safer choice."
        )

    @pytest.mark.parametrize("strategy", _builtin_keys())
    def test_a_refusal_is_not_dressed_up_as_a_result(self, strategy):
        # If it refused, nothing downstream may present a number: a final
        # balance equal to the start is still a number somebody will read.
        stats = _walk(strategy)
        if not stats.unsupported_reason:
            pytest.skip("this strategy walks; the refusal shape is not its case")

        assert stats.trades == 0
        assert stats.total_pnl == 0.0


class TestFixedRr:
    """The baseline, and the one the operator reaches for first.

    Its own summary defines it exactly: "one stop, one target, both set at the
    broker. No partial closes, no breakeven move, no trailing." There is no
    judgement to exercise, which is why it is implemented here rather than
    refused with the EA-managed ones.
    """

    def test_it_walks(self):
        assert _walk("fixed_rr").trades == 1

    def test_it_takes_the_first_target_and_stops(self):
        stats = _walk("fixed_rr")
        trade = stats.trade_list[0]

        assert trade.outcome == "tp1_only"
        assert trade.close_price == pytest.approx(4010.0)

    def test_it_closes_the_whole_position_at_that_target(self):
        # The distinguishing feature. A scale-out would leave a runner on and
        # keep going; fixed R:R is done.
        trade = _walk("fixed_rr").trade_list[0]

        assert trade.pnl_usd > 0
        assert trade.pnl_pts == pytest.approx(4010.0 - trade.fill_price, abs=0.01)

    def test_a_stop_hit_is_a_loss_of_the_whole_position(self):
        falling = [{"ts": _TS0, "high": 4001.0, "low": 3999.0,
                    "open": 4000.0, "close": 4000.0}]
        falling += [{"ts": _TS0 + 60, "high": 4000.0, "low": 3985.0,
                     "open": 4000.0, "close": 3985.0}]
        stats = bt.run_backtest([_signal()], falling, ["fixed_rr"],
                                starting_balance=1000.0, risk_pct=1.0,
                                spread_pts=0.4, commission_per_lot=0.0)["fixed_rr"]
        trade = stats.trade_list[0]

        assert trade.outcome == "sl"
        assert trade.pnl_usd < 0

    def test_the_stop_wins_a_bar_that_touches_both(self):
        # The conservative convention every other simulator here uses:
        # within one bar, assume the adverse touch resolved first. Anything
        # else makes a backtest flatter than the account it models.
        both = [{"ts": _TS0, "high": 4001.0, "low": 3999.0,
                 "open": 4000.0, "close": 4000.0},
                {"ts": _TS0 + 60, "high": 4015.0, "low": 3985.0,
                 "open": 4000.0, "close": 4010.0}]
        stats = bt.run_backtest([_signal()], both, ["fixed_rr"],
                                starting_balance=1000.0, risk_pct=1.0,
                                spread_pts=0.4, commission_per_lot=0.0)["fixed_rr"]

        assert stats.trade_list[0].outcome == "sl"

    def test_it_times_out_rather_than_running_forever(self):
        flat = [{"ts": _TS0 + i * 60, "high": 4001.0, "low": 3999.0,
                 "open": 4000.0, "close": 4000.0} for i in range(40)]
        trade = bt.run_backtest([_signal()], flat, ["fixed_rr"],
                                starting_balance=1000.0, risk_pct=1.0,
                                spread_pts=0.4, commission_per_lot=0.0
                                )["fixed_rr"].trade_list[0]

        assert trade.outcome == "timeout"

    def test_a_signal_with_no_target_is_not_a_fixed_rr_trade(self):
        # One stop and one target. With no target there is nothing to be
        # fixed about, and inventing one would be inventing the strategy.
        stats = bt.run_backtest(
            [_signal(tp1=None, tp2=None, tp3=None)], _rising_candles(),
            ["fixed_rr"], starting_balance=1000.0, risk_pct=1.0,
            spread_pts=0.4, commission_per_lot=0.0,
        )["fixed_rr"]

        assert stats.trades == 0
        assert stats.unsupported_reason == ""


class TestTheOnesThisWalkWillNotModel:
    """Five strategies are managed by the Expert Advisor, not by Python.

    Their behaviour lives in the EA's own Manage* routines
    (`services/broker/ea_bridge`), not in any Python code this walk could
    mirror. Re-deriving them from the EA's source would produce plausible
    numbers used to decide what trades real money, so they refuse instead and
    the refusal names the reason. Implementing them faithfully is the owner's
    call -- see docs/simon-handover/.
    """

    EA_MANAGED = ["scalp_runner", "gold_diggers_copy", "orb_fixed",
                  "adaptive_runner_2", "limit_runner"]

    @pytest.mark.parametrize("strategy", EA_MANAGED)
    def test_it_refuses_out_loud(self, strategy):
        stats = _walk(strategy)

        assert stats.unsupported_reason
        assert stats.trades == 0

    @pytest.mark.parametrize("strategy", EA_MANAGED)
    def test_the_reason_says_where_the_behaviour_lives(self, strategy):
        # A refusal an operator cannot act on is only slightly better than
        # silence. This one points at the EA.
        assert "EA" in _walk(strategy).unsupported_reason

    def test_they_are_all_still_in_the_picker(self):
        # Refusing is not hiding. The operator should see that the strategy
        # exists and that this tool cannot measure it, rather than wonder
        # where it went.
        offered = set(_builtin_keys())

        assert set(self.EA_MANAGED) <= offered


class TestTheTickWalk:
    """The same rule, on the other granularity.

    `_simulate_ticks` returns None for EVERY built-in strategy -- its
    docstring says the picker "has offered EA templates exclusively since item
    7", which was true of the NiceGUI page and is not true of the React one.
    The form offers Candles or Ticks against the whole catalogue, so before
    2026-09-19 choosing Ticks with any built-in strategy produced a full table
    of zeros with no explanation at all.
    """

    def _ticks(self) -> list[dict]:
        return [{"ts": _TS0 + i, "bid": 4000.0 + i, "ask": 4000.2 + i}
                for i in range(200)]

    @pytest.mark.parametrize("strategy", _builtin_keys())
    def test_a_builtin_on_ticks_says_why_it_has_no_numbers(self, strategy):
        stats = bt.run_backtest_ticks(
            [_signal()], self._ticks(), [strategy],
            starting_balance=1000.0, spread_pts=0.4, commission_per_lot=0.0,
        )[strategy]

        assert stats.trades > 0 or stats.unsupported_reason

    def test_the_tick_reason_names_ticks_rather_than_blaming_the_strategy(self):
        # "Scale Out cannot be simulated" would be wrong: it simulates fine on
        # candles. What cannot be done is simulating it on ticks.
        reason = bt.run_backtest_ticks(
            [_signal()], self._ticks(), ["scale_out"],
            starting_balance=1000.0, spread_pts=0.4, commission_per_lot=0.0,
        )["scale_out"].unsupported_reason

        assert "tick" in reason.lower()

    def test_a_template_still_walks_on_ticks(self):
        # The refusal must not swallow the case the tick walk exists for.
        from backend.src.services.backtest import refusals

        assert refusals.builtin("template:Whatever", tick_mode=True) == ""
