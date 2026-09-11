"""Cross-asset correlation, and what it means for exposure.

Section 5.9 of docs/todo/reversal-engine/200. Everything this app does is a
single-instrument bet on gold, and `_MAX_OPEN_SIGNALS = 6` counts SIGNALS,
not exposure: six open XAUUSD longs are one position of six times the size.

Full multi-symbol TRADING is a much larger piece of work (the bridge binds
one symbol at module level, and so do the EA and the trade schema). What is
available today with no new plumbing is multi-symbol market DATA --
`get_candles_for_symbol` is wired end to end -- which is what makes both
cross-asset context and a correlation-aware exposure number possible.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import correlation as co


def candles(prices, start=1000.0, step=3600.0):
    return [{"ts": start + i * step, "close": p} for i, p in enumerate(prices)]


class TestReturns:
    def test_returns_are_per_bar_and_one_shorter_than_the_series(self):
        r = co.returns(candles([100.0, 101.0, 102.01]))
        assert len(r) == 2
        assert r[0][1] == pytest.approx(0.01)

    def test_a_zero_or_missing_close_breaks_the_chain_rather_than_dividing(self):
        r = co.returns(candles([100.0, 0.0, 102.0]))
        assert all(abs(v) < 10 for _ts, v in r)

    def test_a_single_candle_has_no_returns(self):
        assert co.returns(candles([100.0])) == []


class TestAlignment:
    def test_only_timestamps_present_in_both_series_are_compared(self):
        """Gold trades hours that equities do not. Comparing unaligned
        series pairs Monday's gold with Friday's S&P and calls the result a
        correlation."""
        a = co.returns(candles([100.0, 101.0, 102.0], start=0.0))
        b = co.returns(candles([50.0, 50.5], start=3600.0))
        xs, ys = co.align(a, b)
        assert len(xs) == len(ys) == 1

    def test_no_overlap_gives_nothing_to_compare(self):
        a = co.returns(candles([100.0, 101.0], start=0.0))
        b = co.returns(candles([50.0, 51.0], start=1_000_000.0))
        assert co.align(a, b) == ([], [])


class TestPearson:
    def test_a_perfectly_matched_pair_correlates_at_one(self):
        assert co.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_a_mirrored_pair_correlates_at_minus_one(self):
        assert co.pearson([1.0, 2.0, 3.0], [-2.0, -4.0, -6.0]) == pytest.approx(-1.0)

    def test_a_flat_series_has_no_correlation_rather_than_zero(self):
        """Zero means "moves independently", which is a claim. A series that
        never moved supports no claim at all."""
        assert co.pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None

    def test_too_few_points_is_refused(self):
        assert co.pearson([1.0], [2.0]) is None


class TestEffectiveExposure:
    def test_two_uncorrelated_positions_are_less_than_their_sum(self):
        """The entire point. Adding lots across instruments as though they
        were one position overstates risk; ignoring correlation entirely
        understates it. Neither is the number a cap should use."""
        e = co.effective_exposure({"A": 1.0, "B": 1.0}, {("A", "B"): 0.0})
        assert 1.0 < e < 2.0

    def test_two_perfectly_correlated_positions_add_up(self):
        e = co.effective_exposure({"A": 1.0, "B": 1.0}, {("A", "B"): 1.0})
        assert e == pytest.approx(2.0)

    def test_a_perfect_hedge_nets_to_nothing(self):
        e = co.effective_exposure({"A": 1.0, "B": -1.0}, {("A", "B"): 1.0})
        assert e == pytest.approx(0.0)

    def test_same_instrument_positions_are_simply_additive(self):
        """Six open XAUUSD longs are one position of six times the size,
        which is the specific thing _MAX_OPEN_SIGNALS does not know."""
        assert co.effective_exposure({"XAUUSD": 0.06}, {}) == pytest.approx(0.06)

    def test_an_unknown_pair_is_assumed_correlated_not_independent(self):
        """The conservative default. Assuming independence for a pair
        nobody measured is how a risk system discovers in a drawdown that
        everything it held was the same trade."""
        e = co.effective_exposure({"A": 1.0, "B": 1.0}, {})
        assert e == pytest.approx(2.0)
