"""Fit the stop and the target to what price actually did.

Section 1.1 and phase 2 of docs/todo/reversal-engine/200. The generator's
current geometry was reverse engineered from a Telegram channel's messages:
targets at fixed point offsets, a stop that varies with level score, so TP1 is
0.75R on a weak level and 0.43R on a strong one. The owner's directive on
2026-09-11 retired the requirement to resemble that channel, so the barriers
get fitted to this engine's own excursion distribution instead.

The refusals matter as much as the fit. A barrier fitted on nine trades is a
number with a decimal point and no information, and it would be acted on.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import barrier_fit as bfit
from backend.src.services.market import exit_replay as er


def obs(mfe, mae, outcome="win", atr=8.0, close_time=0.0):
    return {"mfe_pts": mfe, "mae_pts": mae, "outcome": outcome,
            "atr": atr, "close_time": close_time}


class TestTheStopIsFittedToWhatWINNERSSurvive:
    def test_the_stop_covers_the_chosen_quantile_of_winner_drawdown(self):
        """Winners that go 1, 2 and 3 points against you before working need
        a 3 point stop to keep all three. At the 100th percentile that is the
        answer; the point of the quantile is to choose how many to keep."""
        sample = [obs(10, 1.0), obs(10, 2.0), obs(10, 3.0)]
        fit = bfit.fit_barriers(sample, min_sample=3, stop_quantile=1.0)
        assert fit.stop_pts == pytest.approx(3.0)

    def test_a_lower_quantile_gives_a_tighter_stop(self):
        sample = [obs(10, 1.0), obs(10, 2.0), obs(10, 3.0), obs(10, 9.0)]
        wide = bfit.fit_barriers(sample, min_sample=3, stop_quantile=1.0)
        tight = bfit.fit_barriers(sample, min_sample=3, stop_quantile=0.5)
        assert tight.stop_pts < wide.stop_pts

    def test_losers_do_not_set_the_stop(self):
        """A loser's MAE is bounded by wherever the stop happened to be, so
        fitting on it fits the old rule rather than the market. This is the
        measurement bias reversal-engine/020 warns about at the end."""
        winners = [obs(10, 1.0), obs(10, 1.5), obs(10, 2.0)]
        with_losers = winners + [obs(0, 30.0, outcome="loss")] * 20
        assert (bfit.fit_barriers(with_losers, min_sample=3).stop_pts
                == pytest.approx(bfit.fit_barriers(winners, min_sample=3).stop_pts))


class TestTheTargetIsFittedToWhereWinnersActuallyReach:
    def test_the_target_is_a_quantile_of_winner_excursion(self):
        sample = [obs(4.0, 1.0), obs(6.0, 1.0), obs(20.0, 1.0)]
        fit = bfit.fit_barriers(sample, min_sample=3, target_quantile=0.5)
        assert fit.target_pts == pytest.approx(6.0)

    def test_the_fit_reports_the_payoff_it_implies(self):
        sample = [obs(6.0, 3.0)] * 5
        fit = bfit.fit_barriers(sample, min_sample=3, stop_quantile=1.0,
                                target_quantile=1.0)
        assert fit.rr == pytest.approx(2.0)


class TestVolatilityNormalisation:
    def test_barriers_are_also_reported_as_atr_multiples(self):
        """A fixed point stop is a different amount of risk on a quiet day
        and a violent one. The ATR multiple is what the EA template's
        use_dynamic_atr consumes, so this is the number that ships."""
        sample = [obs(8.0, 4.0, atr=8.0)] * 5
        fit = bfit.fit_barriers(sample, min_sample=3, stop_quantile=1.0,
                                target_quantile=1.0)
        assert fit.stop_atr_mult == pytest.approx(0.5)
        assert fit.target_atr_mult == pytest.approx(1.0)

    def test_a_zero_atr_row_is_excluded_rather_than_dividing_by_zero(self):
        sample = [obs(8.0, 4.0, atr=8.0)] * 4 + [obs(8.0, 4.0, atr=0.0)]
        fit = bfit.fit_barriers(sample, min_sample=3)
        assert fit.n_atr == 4


class TestItRefusesToFitOnTooLittle:
    def test_below_the_minimum_sample_it_returns_no_fit_and_says_why(self):
        fit = bfit.fit_barriers([obs(10, 1.0)] * 5, min_sample=30)
        assert fit.stop_pts is None
        assert "sample" in fit.refusal.lower()

    def test_no_winners_at_all_is_a_refusal_not_a_zero_stop(self):
        fit = bfit.fit_barriers([obs(0, 5.0, outcome="loss")] * 50, min_sample=3)
        assert fit.stop_pts is None


class TestPolicySweep:
    """The production form of tools/exit_policy_lab.py, which found that a
    breakeven move reduced expectancy in 8 of 8 configurations tested."""

    def _trade(self, prices, direction="BUY", entry=3300.0, t0=0.0):
        return {"path": [(t0 + i, p, p) for i, p in enumerate(prices)],
                "entry": entry, "direction": direction, "close_time": t0}

    def test_expectancy_is_the_mean_r_over_every_trade(self):
        trades = [self._trade([3300.0, 3306.0]), self._trade([3300.0, 3297.0])]
        policy = er.ExitPolicy(stop_pts=3.0, target_pts=6.0)
        assert bfit.expectancy(trades, policy) == pytest.approx((2.0 - 1.0) / 2)

    def test_a_trade_with_no_path_is_excluded_not_counted_as_zero(self):
        trades = [self._trade([3300.0, 3306.0]), {"path": [], "entry": 3300.0,
                                                  "direction": "BUY", "close_time": 0}]
        assert bfit.expectancy(trades, er.ExitPolicy(stop_pts=3.0,
                                                     target_pts=6.0)) == pytest.approx(2.0)

    def test_the_sweep_ranks_configurations(self):
        trades = [self._trade([3300.0, 3303.0, 3308.0]) for _ in range(10)]
        rows = bfit.sweep(trades, stops=(3.0, 6.0), targets=(6.0,))
        assert rows[0].stop_pts == 3.0
        assert rows[0].expectancy > rows[1].expectancy

    def test_the_sweep_reports_a_bootstrap_interval_that_can_straddle_zero(self):
        trades = ([self._trade([3300.0, 3306.0])] * 5
                  + [self._trade([3300.0, 3297.0])] * 5)
        row = bfit.sweep(trades, stops=(3.0,), targets=(6.0,), bootstrap=200)[0]
        assert row.ci_low < row.expectancy < row.ci_high

    def test_the_sweep_splits_chronologically_so_a_rank_can_be_checked_twice(self):
        early = [self._trade([3300.0, 3306.0], t0=1.0)] * 6
        late = [self._trade([3300.0, 3297.0], t0=9_000.0)] * 6
        row = bfit.sweep(early + late, stops=(3.0,), targets=(6.0,))[0]
        assert row.first_half == pytest.approx(2.0)
        assert row.second_half == pytest.approx(-1.0)

    def test_cost_is_charged_so_a_tighter_stop_pays_proportionally_more(self):
        trades = [self._trade([3300.0, 3312.0])] * 5
        rows = bfit.sweep(trades, stops=(3.0, 6.0), targets=(12.0,), cost_pts=0.6)
        by_stop = {r.stop_pts: r.expectancy for r in rows}
        assert by_stop[3.0] == pytest.approx(4.0 - 0.2)
        assert by_stop[6.0] == pytest.approx(2.0 - 0.1)
