"""Counterfactual exit replay: what a different exit rule would have returned
on the path a trade actually walked.

This is section 5.3 of docs/todo/reversal-engine/200, and it is what settles
item 030 (wins closed at 0.642R) without risking a pound. The rules below are
not conveniences -- each one is a place where a plausible implementation gives
a wrong answer that looks right.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import exit_replay as er


def tick_path(prices, start=1000.0, step=1.0):
    return [(start + i * step, p, p) for i, p in enumerate(prices)]


class TestFixedBarriers:
    def test_a_long_that_reaches_its_target_returns_the_reward_ratio(self):
        path = tick_path([3300.0, 3303.0, 3306.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "target"
        assert r.r_multiple == pytest.approx(2.0)

    def test_a_long_that_reaches_its_stop_returns_minus_one(self):
        path = tick_path([3300.0, 3298.0, 3297.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "stop"
        assert r.r_multiple == pytest.approx(-1.0)

    def test_a_short_is_the_mirror_image(self):
        path = tick_path([3300.0, 3297.0, 3294.0])
        r = er.replay(path, entry=3300.0, direction="SELL",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "target"
        assert r.r_multiple == pytest.approx(2.0)

    def test_the_stop_wins_when_it_comes_first_in_time(self):
        """On a TICK path there is no ambiguity to resolve: whichever level
        the path reaches first is the one that happened."""
        path = tick_path([3300.0, 3297.0, 3310.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "stop"

    def test_the_target_wins_when_it_comes_first_in_time(self):
        path = tick_path([3300.0, 3306.0, 3290.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "target"

    def test_an_ambiguous_bar_resolves_the_stop_first(self):
        """A bar that spans both levels is unresolvable. Taking the stop is
        the pessimistic choice and deliberately matches
        backtest/template_simulator.py -- a replay that flatters itself is the
        failure mode worth avoiding, because its numbers get trusted."""
        path = er_bar_path_spanning_both()
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "stop"

    def test_an_unfinished_trade_is_marked_at_the_last_price(self):
        path = tick_path([3300.0, 3301.5])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0))
        assert r.reason == "path_end"
        assert r.r_multiple == pytest.approx(0.5)


def er_bar_path_spanning_both():
    from backend.src.services.market import price_path as pp
    return pp.build_bar_path([{"ts": 1.0, "high": 3307.0, "low": 3296.0}])


class TestBreakeven:
    """Item 030's suspect. The whole point of the replay is to price this."""

    def test_breakeven_arms_only_after_its_trigger_and_then_scratches(self):
        path = tick_path([3300.0, 3303.0, 3300.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0,
                                           be_trigger_pts=3.0))
        assert r.reason == "breakeven"
        assert r.r_multiple == pytest.approx(0.0)

    def test_without_the_trigger_the_same_path_keeps_running(self):
        """Same path, no breakeven rule: the trade survives the retrace and
        goes on to its target. This pair IS the measurement item 030 needs."""
        path = tick_path([3300.0, 3303.0, 3300.0, 3309.0])
        with_be = er.replay(path, entry=3300.0, direction="BUY",
                            policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0,
                                                 be_trigger_pts=3.0))
        without = er.replay(path, entry=3300.0, direction="BUY",
                            policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0))
        assert with_be.r_multiple == pytest.approx(0.0)
        assert without.r_multiple == pytest.approx(3.0)

    def test_breakeven_latches_and_is_not_re_tested_on_every_point(self):
        """The EA arms off `triggered[]`, not a live price test, because
        re-asking "is price beyond the trigger right now" un-arms the move on
        any retrace -- 141 trades over a month never reached breakeven and
        closed a mean 66.7 pips below entry. This replay latches the same way,
        or it would price a rule the EA does not implement."""
        path = tick_path([3300.0, 3303.0, 3301.0, 3299.9])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0,
                                           be_trigger_pts=3.0))
        assert r.reason == "breakeven"

    def test_a_breakeven_buffer_locks_a_small_gain(self):
        path = tick_path([3300.0, 3303.0, 3300.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0,
                                           be_trigger_pts=3.0, be_buffer_pts=0.1))
        assert r.r_multiple == pytest.approx(0.1 / 3.0)


class TestTrail:
    def test_the_trail_does_nothing_before_activation(self):
        path = tick_path([3300.0, 3302.0, 3297.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, trail_activation_pts=5.0,
                                           trail_distance_pts=2.0))
        assert r.reason == "stop"
        assert r.r_multiple == pytest.approx(-1.0)

    def test_after_activation_the_stop_follows_the_high(self):
        path = tick_path([3300.0, 3306.0, 3304.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, trail_activation_pts=5.0,
                                           trail_distance_pts=2.0))
        assert r.reason == "trail"
        assert r.r_multiple == pytest.approx(4.0 / 3.0)

    def test_the_trailing_stop_never_moves_backwards(self):
        path = tick_path([3300.0, 3308.0, 3306.4, 3306.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, trail_activation_pts=5.0,
                                           trail_distance_pts=2.0))
        assert r.r_multiple == pytest.approx(6.0 / 3.0)


class TestPartials:
    def test_half_off_at_the_target_and_the_runner_pays_the_rest(self):
        path = tick_path([3300.0, 3303.0, 3306.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=3.0,
                                           target_frac=0.5, runner_target_pts=6.0))
        assert r.reason == "runner"
        assert r.r_multiple == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)

    def test_a_partial_already_banked_survives_a_later_stop(self):
        """This is the entire argument for taking something off at 1R. The
        trade still ends at the stop, but it does not end at -1R."""
        path = tick_path([3300.0, 3303.0, 3297.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=3.0,
                                           target_frac=0.5, runner_target_pts=9.0))
        assert r.reason == "stop"
        assert r.r_multiple == pytest.approx(0.5 * 1.0 + 0.5 * -1.0)


class TestCostAndTime:
    def test_the_round_trip_cost_is_charged_in_r(self):
        """A 0.6pt round trip against a 3pt stop is 20% of R. Section 5.1:
        an edge is net of measured cost or it is not an edge."""
        path = tick_path([3300.0, 3306.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=6.0,
                                           cost_pts=0.6))
        assert r.r_multiple == pytest.approx(2.0 - 0.2)

    def test_a_time_stop_closes_at_the_prevailing_price(self):
        path = tick_path([3300.0, 3301.0, 3302.0, 3303.0], start=0.0, step=60.0)
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0,
                                           time_stop_s=120.0))
        assert r.reason == "time"
        assert r.r_multiple == pytest.approx(2.0 / 3.0)


class TestExcursionIsReportedAlongside:
    def test_the_result_carries_the_full_path_excursion(self):
        """Reported over the WHOLE path, not truncated at the exit: the point
        of the replay is to see what was left on the table after the rule
        closed the trade."""
        path = tick_path([3300.0, 3303.0, 3297.0, 3320.0])
        r = er.replay(path, entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=3.0, target_pts=9.0))
        assert r.reason == "stop"
        assert r.mfe_pts == pytest.approx(20.0)
        assert r.mae_pts == pytest.approx(3.0)


class TestGuards:
    def test_a_zero_stop_is_refused_rather_than_dividing_by_zero(self):
        with pytest.raises(ValueError):
            er.replay(tick_path([3300.0]), entry=3300.0, direction="BUY",
                      policy=er.ExitPolicy(stop_pts=0.0, target_pts=6.0))

    def test_an_empty_path_returns_nothing_rather_than_a_zero_r(self):
        assert er.replay([], entry=3300.0, direction="BUY",
                         policy=er.ExitPolicy(stop_pts=3.0)) is None
