"""Volume profile: where trade actually happened, by price.

Section 4.1 of docs/todo/reversal-engine/200. The engine's level map has no
concept of where volume transacted, only where price turned. High volume
nodes are where price returns; low volume nodes are where it travels fast,
which is a statement about where a TARGET should sit, not just an entry.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import volume_profile as vp


def candle(low, high, volume=100):
    return {"low": low, "high": high, "tick_volume": volume}


class TestTheProfile:
    def test_volume_is_spread_across_the_range_each_candle_covered(self):
        """A candle is not a point. Assigning its whole volume to the close
        would put the busiest price wherever the candle happened to end,
        which on a wide bar is nowhere near where the trade occurred."""
        prof = vp.build([candle(100.0, 110.0, volume=100)], bins=10)
        assert len(prof.bins) == 10
        assert all(b.volume == pytest.approx(10.0) for b in prof.bins)

    def test_the_point_of_control_is_the_busiest_price(self):
        candles = [candle(100.0, 110.0, 100), candle(104.0, 106.0, 900)]
        prof = vp.build(candles, bins=10)
        assert 104.0 <= prof.poc <= 106.0

    def test_the_value_area_covers_the_requested_share_of_volume(self):
        candles = [candle(100.0, 110.0, 100), candle(104.5, 105.5, 900)]
        prof = vp.build(candles, bins=10, value_area=0.70)
        assert prof.val <= prof.poc <= prof.vah
        assert prof.value_area_volume / prof.total_volume >= 0.70

    def test_the_value_area_grows_when_more_coverage_is_asked_for(self):
        candles = [candle(100.0, 110.0, 100), candle(104.5, 105.5, 400)]
        narrow = vp.build(candles, bins=20, value_area=0.50)
        wide = vp.build(candles, bins=20, value_area=0.95)
        assert (wide.vah - wide.val) >= (narrow.vah - narrow.val)


class TestNodes:
    def test_a_low_volume_node_is_a_price_trade_passed_through(self):
        candles = [candle(100.0, 110.0, 100), candle(100.0, 102.0, 900),
                   candle(108.0, 110.0, 900)]
        prof = vp.build(candles, bins=10)
        lvns = vp.low_volume_nodes(prof, threshold_frac=0.5)
        assert any(104.0 <= p <= 106.0 for p in lvns)

    def test_a_high_volume_node_is_a_price_trade_kept_returning_to(self):
        candles = [candle(100.0, 110.0, 100), candle(104.0, 106.0, 900)]
        prof = vp.build(candles, bins=10)
        assert any(104.0 <= p <= 106.0 for p in vp.high_volume_nodes(prof))


class TestItRefusesRatherThanGuesses:
    def test_no_candles_gives_no_profile(self):
        assert vp.build([]) is None

    def test_a_flat_range_gives_no_profile_rather_than_dividing_by_zero(self):
        assert vp.build([candle(100.0, 100.0, 50)]) is None

    def test_a_candle_with_no_volume_field_counts_as_one_unit_of_activity(self):
        """MT5 serves `tick_volume` for a CFD, a COUNT of quote changes
        rather than traded size. A feed that omits it entirely still gives a
        usable time-at-price profile if every candle counts equally, and
        that is strictly better than refusing to build one -- but it is a
        different measurement and the profile says which it is."""
        prof = vp.build([{"low": 100.0, "high": 110.0}], bins=10)
        assert prof.total_volume == pytest.approx(1.0)
        assert prof.volume_is_proxy is True
