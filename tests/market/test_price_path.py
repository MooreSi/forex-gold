"""The price path a trade actually saw, built from broker ticks.

These are the foundation of every measurement in
docs/todo/reversal-engine/200: excursion backfill, counterfactual exit
replay, and barrier fitting all walk a path built here. A defect here is
silently wrong numbers everywhere downstream, so the side-of-the-book
question is pinned explicitly rather than left to the mid price.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import price_path as pp


class TestExitSide:
    """A long is closed at the BID, a short at the ASK. Using the mid
    understates the loss on both, by half the spread, on every single trade
    -- which is precisely the leakage reversal-engine/020 is hunting."""

    def test_a_long_walks_the_bid(self):
        ticks = [{"time": 1.0, "bid": 3300.0, "ask": 3300.4}]
        assert pp.build_tick_path(ticks, "BUY") == [(1.0, 3300.0, 3300.0)]

    def test_a_short_walks_the_ask(self):
        ticks = [{"time": 1.0, "bid": 3300.0, "ask": 3300.4}]
        assert pp.build_tick_path(ticks, "SELL") == [(1.0, 3300.4, 3300.4)]

    def test_a_tick_missing_a_side_is_dropped_not_guessed(self):
        ticks = [{"time": 1.0, "bid": 0.0, "ask": 3300.4},
                 {"time": 2.0, "bid": 3300.0, "ask": 3300.4}]
        assert pp.build_tick_path(ticks, "BUY") == [(2.0, 3300.0, 3300.0)]

    def test_the_path_is_sorted_by_time(self):
        ticks = [{"time": 5.0, "bid": 3301.0, "ask": 3301.4},
                 {"time": 1.0, "bid": 3300.0, "ask": 3300.4}]
        assert [p[0] for p in pp.build_tick_path(ticks, "BUY")] == [1.0, 5.0]


class TestBarPath:
    def test_a_bar_keeps_its_high_and_low(self):
        bars = [{"ts": 1.0, "high": 3305.0, "low": 3299.0}]
        assert pp.build_bar_path(bars) == [(1.0, 3305.0, 3299.0)]

    def test_a_bar_point_is_ambiguous_and_a_tick_point_is_not(self):
        assert pp.is_ambiguous((1.0, 3305.0, 3299.0)) is True
        assert pp.is_ambiguous((1.0, 3300.0, 3300.0)) is False


class TestExcursion:
    def test_a_long_measures_favourable_up_and_adverse_down(self):
        path = [(1.0, 3300.0, 3300.0), (2.0, 3304.0, 3304.0), (3.0, 3297.0, 3297.0)]
        mfe, mae = pp.excursion(path, entry=3300.0, direction="BUY")
        assert mfe == pytest.approx(4.0)
        assert mae == pytest.approx(3.0)

    def test_a_short_measures_favourable_down_and_adverse_up(self):
        path = [(1.0, 3300.0, 3300.0), (2.0, 3304.0, 3304.0), (3.0, 3297.0, 3297.0)]
        mfe, mae = pp.excursion(path, entry=3300.0, direction="SELL")
        assert mfe == pytest.approx(3.0)
        assert mae == pytest.approx(4.0)

    def test_excursion_is_never_negative(self):
        """A trade that only ever went one way has a zero excursion on the
        other side, not a negative one. measure_repo.record_excursion stores
        positive distances and MAXes them, so a negative would be silently
        clamped to the previous watermark and look like no observation."""
        path = [(1.0, 3302.0, 3302.0), (2.0, 3304.0, 3304.0)]
        mfe, mae = pp.excursion(path, entry=3300.0, direction="BUY")
        assert mfe == pytest.approx(4.0)
        assert mae == 0.0

    def test_a_bar_path_uses_both_extremes_of_each_bar(self):
        path = pp.build_bar_path([{"ts": 1.0, "high": 3306.0, "low": 3294.0}])
        mfe, mae = pp.excursion(path, entry=3300.0, direction="BUY")
        assert mfe == pytest.approx(6.0)
        assert mae == pytest.approx(6.0)

    def test_an_empty_path_measures_nothing_rather_than_zero(self):
        """None, not 0.0. A signal with no tick coverage must not be recorded
        as one that never moved -- that would poison the barrier fit with
        fabricated 0.0 excursions, which is the same class of error as the
        fabricated $0.00 closes in reversal-engine/010."""
        assert pp.excursion([], entry=3300.0, direction="BUY") is None
