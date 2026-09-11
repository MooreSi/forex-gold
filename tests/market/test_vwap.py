"""VWAP: the volume-weighted average price, and its bands.

Section 4.1. VWAP is the single most widely referenced intraday price on a
real desk and this app has none. Institutional execution is benchmarked
against it, which is precisely why price reacts around it.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import vwap as v


def c(ts, high, low, close, volume=100):
    return {"ts": ts, "high": high, "low": low, "close": close,
            "tick_volume": volume}


class TestVwap:
    def test_it_weights_the_typical_price_by_volume(self):
        candles = [c(1, 102, 98, 100, volume=1), c(2, 202, 198, 200, volume=3)]
        # typical prices are 100 and 200; weighted 1:3 -> 175
        assert v.vwap(candles).vwap == pytest.approx(175.0)

    def test_only_candles_from_the_anchor_onward_are_counted(self):
        candles = [c(1, 102, 98, 100), c(10, 202, 198, 200)]
        assert v.vwap(candles, anchor_ts=5).vwap == pytest.approx(200.0)

    def test_bands_sit_symmetrically_either_side(self):
        candles = [c(1, 100, 100, 100, 1), c(2, 110, 110, 110, 1)]
        r = v.vwap(candles)
        assert r.upper_1sd - r.vwap == pytest.approx(r.vwap - r.lower_1sd)
        assert r.upper_2sd - r.vwap == pytest.approx(2 * (r.upper_1sd - r.vwap))

    def test_a_single_candle_has_no_dispersion_so_the_bands_collapse(self):
        r = v.vwap([c(1, 100, 100, 100, 1)])
        assert r.sd == pytest.approx(0.0)
        assert r.upper_1sd == pytest.approx(r.vwap)

    def test_no_candles_gives_nothing(self):
        assert v.vwap([]) is None

    def test_an_anchor_after_every_candle_gives_nothing(self):
        assert v.vwap([c(1, 102, 98, 100)], anchor_ts=99) is None

    def test_zero_volume_candles_fall_back_to_equal_weighting(self):
        """A zero-volume bar is a feed artefact, not a statement that no
        trade happened at that price. Dropping the weighting entirely is
        honest; dividing by zero is not."""
        r = v.vwap([c(1, 102, 98, 100, volume=0), c(2, 202, 198, 200, volume=0)])
        assert r.vwap == pytest.approx(150.0)
        assert r.volume_is_proxy is True
