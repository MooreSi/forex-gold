"""The candidate trade: its reward-to-risk, what invalidates it, and the two
cash figures the position box prints.

This is the last gate before a setup reaches a button that can place it, so
what is asserted here is mostly refusals. A setup that is merely WRONG -- stop
on the far side of entry, target behind price, 1:0.8 dressed up as a plan --
renders as a perfectly tidy card with plausible numbers. Nothing about the
screen says no. This module is what says no.

Two rules come straight from the method and are not negotiable here:

- **Minimum 1:2.** Below it, Alex G's arithmetic stops working: the win rate
  that makes the method profitable assumes the winners are twice the losers.
- **The stop is beyond the zone, the target at the next zone.** So a stop that
  is not on the protective side of the entry is not a tight stop, it is a
  different trade.

`money_at_risk` and `money_at_target` deliberately do not do their own
arithmetic. They call `fees_sizing.pnl`, which is the app's one P&L function --
golden rule 5 says money maths is not a place to be clever, and a second
implementation here would be a second answer to "what is this trade worth".
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import setup


class TestBuild:
    def test_a_long_measures_risk_down_and_reward_up(self):
        s = setup.build("BUY", entry=2000.0, stop_loss=1990.0, take_profit=2030.0)

        assert s["risk"] == 10.0
        assert s["reward"] == 30.0
        assert s["rr"] == 3.0

    def test_a_short_measures_risk_up_and_reward_down(self):
        s = setup.build("SELL", entry=2000.0, stop_loss=2010.0, take_profit=1970.0)

        assert s["risk"] == 10.0
        assert s["reward"] == 30.0
        assert s["rr"] == 3.0

    def test_direction_is_normalised_so_the_browser_cannot_send_a_new_one(self):
        assert setup.build("buy", 2000.0, 1990.0, 2030.0)["direction"] == "BUY"

    def test_a_zero_risk_distance_reports_no_ratio_rather_than_infinity(self):
        """An entry and a stop at the same price is a broken setup, not an
        infinitely good one. `inf` would sort to the top of any ranking and
        render as the best trade on the page."""
        s = setup.build("BUY", entry=2000.0, stop_loss=2000.0, take_profit=2030.0)

        assert s["rr"] is None


class TestInvalidations:
    def test_a_sound_two_to_one_setup_has_none(self):
        s = setup.build("BUY", 2000.0, 1990.0, 2020.0)

        assert setup.invalidations(s) == []

    def test_a_long_whose_stop_is_above_its_entry_is_refused(self):
        s = setup.build("BUY", entry=2000.0, stop_loss=2010.0, take_profit=2040.0)

        reasons = setup.invalidations(s)

        assert reasons and any("stop" in r.lower() for r in reasons)

    def test_a_long_whose_target_is_below_its_entry_is_refused(self):
        s = setup.build("BUY", entry=2000.0, stop_loss=1990.0, take_profit=1995.0)

        assert any("target" in r.lower() for r in setup.invalidations(s))

    def test_a_short_is_checked_the_other_way_round(self):
        good = setup.build("SELL", 2000.0, 2010.0, 1980.0)
        bad = setup.build("SELL", 2000.0, 1990.0, 1980.0)

        assert setup.invalidations(good) == []
        assert setup.invalidations(bad) != []

    def test_below_the_minimum_reward_to_risk_is_refused_by_name(self):
        """1:1.5 is a real setup that a model will happily propose. It is not
        one of Alex G's, and the refusal says the number so the operator can
        see how far off it was rather than just that something was."""
        s = setup.build("BUY", entry=2000.0, stop_loss=1990.0, take_profit=2015.0)

        reasons = setup.invalidations(s)

        assert len(reasons) == 1
        assert "1.5" in reasons[0] and "2" in reasons[0]

    def test_exactly_the_minimum_is_allowed(self):
        assert setup.invalidations(setup.build("BUY", 2000.0, 1990.0, 2020.0)) == []

    def test_no_candidate_at_all_has_nothing_to_invalidate(self):
        """Called for every read, including the ones where the rules refused
        before a candidate existed. None is "there is no trade", which is not
        the same as "there is a trade and it is broken" -- and answering it
        here rather than with an `if` at each call site is what keeps the
        controller a forwarder."""
        assert setup.invalidations(None) == []

    def test_an_unknown_direction_is_refused(self):
        assert setup.invalidations(setup.build("HOLD", 2000.0, 1990.0, 2020.0)) != []

    def test_a_short_whose_target_is_above_its_entry_is_refused(self):
        """The mirror of the long case, which a single shared branch would
        never catch. A short targeting a level ABOVE its entry has the trade
        inverted, and every number on the card still agrees with itself."""
        s = setup.build("SELL", entry=2000.0, stop_loss=2010.0, take_profit=2030.0)

        assert any("target" in r.lower() for r in setup.invalidations(s))

    def test_a_setup_with_two_things_wrong_reports_both(self):
        """A list rather than the first failure. Fixing whichever one the
        function happened to mention first would just surface the other, one
        round-trip at a time."""
        s = setup.build("BUY", entry=2000.0, stop_loss=2010.0, take_profit=1990.0)

        reasons = setup.invalidations(s)

        assert any("stop must be below" in r for r in reasons)
        assert any("target must be above" in r for r in reasons)

    def test_an_entry_and_a_stop_at_the_same_price_are_refused_in_words(self):
        """`rr` is None there, and a refusal that only said "no ratio" would
        read as a display problem rather than a broken setup."""
        s = setup.build("BUY", entry=2000.0, stop_loss=2000.0, take_profit=2030.0)

        reasons = setup.invalidations(s)

        assert any("same price" in r for r in reasons)

    def test_the_minimum_is_a_parameter_but_its_default_is_the_method(self):
        s = setup.build("BUY", entry=2000.0, stop_loss=1990.0, take_profit=2015.0)

        assert setup.invalidations(s, min_rr=1.5) == []
        assert setup.MIN_RR == 2.0


class TestOrderType:
    def test_an_entry_at_the_current_price_is_a_market_order(self):
        assert setup.order_type_for("BUY", entry=2000.0, price=2000.2,
                                    tolerance=0.5) == "market"

    def test_a_buy_below_the_current_price_rests_as_a_limit(self):
        """The set-and-forget entry: the order waits at the zone for price to
        come back to it, which is the whole reason the method does not need
        anyone watching."""
        assert setup.order_type_for("BUY", entry=1990.0, price=2000.0,
                                    tolerance=0.5) == "limit"

    def test_a_sell_above_the_current_price_rests_as_a_limit(self):
        assert setup.order_type_for("SELL", entry=2010.0, price=2000.0,
                                    tolerance=0.5) == "limit"

    def test_an_entry_price_reached_only_by_chasing_is_named_a_stop(self):
        """A BUY above the market is a buy-stop -- entering as price runs away
        from the zone. It is not part of this method, so it is named rather
        than quietly placed as something else, and `invalidations` refuses it."""
        assert setup.order_type_for("BUY", entry=2010.0, price=2000.0,
                                    tolerance=0.5) == "stop"

    def test_an_unknown_direction_falls_back_to_market_for_invalidations_to_refuse(
            self):
        """Not a real state -- `build` normalises the direction and
        `invalidations` refuses anything that is not BUY or SELL. This is the
        guard that stops a nonsense direction reaching the order type as a
        crash instead of as a readable refusal."""
        assert setup.order_type_for("HOLD", entry=1990.0, price=2000.0,
                                    tolerance=0.5) == "market"

    def test_a_stop_entry_is_an_invalidation(self):
        s = setup.build("BUY", 2010.0, 2000.0, 2035.0, order_type="stop")

        assert any("stop order" in r.lower() for r in setup.invalidations(s))


class TestMoney:
    def test_the_cash_figures_come_from_the_apps_own_pnl_function(self, monkeypatch):
        """Not a mock for convenience -- the point of the test. Golden rule 5:
        there is one P&L function in this app and this module must not grow a
        second one. If the call moves, this goes red."""
        seen = []

        def _pnl(direction, entry, current, lots):
            seen.append((direction, entry, current, lots))
            return 123.0

        monkeypatch.setattr(setup._fees, "pnl", _pnl)
        s = setup.build("BUY", 2000.0, 1990.0, 2030.0)

        assert setup.money_at_risk(s, 0.5) == 123.0
        assert setup.money_at_target(s, 0.5) == 123.0
        assert seen == [("BUY", 2000.0, 1990.0, 0.5), ("BUY", 2000.0, 2030.0, 0.5)]

    def test_the_risk_figure_is_reported_as_a_positive_amount(self):
        """The box says "Amount: 750" under a red band. A minus sign there
        would read as a loss on a loss."""
        s = setup.build("BUY", 2000.0, 1990.0, 2030.0)

        assert setup.money_at_risk(s, 0.5) > 0

    def test_no_lots_is_none_rather_than_zero(self):
        """Zero is a real answer meaning "this trade wins nothing". Not having
        chosen a lot size yet is a different state and the box says so."""
        s = setup.build("BUY", 2000.0, 1990.0, 2030.0)

        assert setup.money_at_risk(s, None) is None
        assert setup.money_at_target(s, 0.0) is None


class TestLotFromRisk:
    def test_it_forwards_to_the_shared_sizing_function_untouched(self, monkeypatch):
        """Position sizing is money maths and this module reimplements none of
        it. `suggest_lot_size` already applies Max Risk per trade % on top of
        Risk per trade %; a local calculation here would silently drop that
        second ceiling."""
        seen = []
        monkeypatch.setattr(setup._fees, "suggest_lot_size",
                            lambda *a: seen.append(a) or 0.07)

        assert setup.lot_from_risk(2000.0, 1990.0, 5000.0, 1.0) == 0.07
        assert seen == [(2000.0, 1990.0, 5000.0, 1.0)]

    @pytest.mark.parametrize("balance", [0.0, -10.0])
    def test_a_non_positive_balance_sizes_nothing(self, balance):
        """Better no number than a lot size computed against an account that
        cannot be read -- that number would go in the box and then to a
        broker."""
        assert setup.lot_from_risk(2000.0, 1990.0, balance, 1.0) is None
