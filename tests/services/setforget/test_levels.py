"""Areas of interest as HORIZONTAL LEVELS -- the 2026-09-24 fix.

The owner reported the page never offered a setup. Replayed against a year of
real gold candles (every 4H close for 240 days, 1,440 reads), the rules
produced **zero** candidates. Most of the blame was one mechanism:

`aoi.zones` spans each swing candle's whole BODY and merges bands that overlap.
A gold Daily body is 50-150 points, a Weekly one several hundred, and in a
trend neighbouring bodies overlap -- so the merge chains them into bands 80-500
points wide. `propose` then (correctly) refuses any band wider than
`MAX_ZONE_WIDTH_PCT` of price. Result: on 24 of 25 sampled dates there was not
one usable zone on the chart, in either direction.

A trader draws a level where price TURNED, several times, at about the same
price. So `levels` clusters the turning points themselves -- each swing
candle's body edge on the wick side, which is where the traders who turned it
closed -- and a cluster may never grow wider than `width_pct` of price. Three
turns inside that band validate it; three scattered turns do not.

A former swing high below price is support and a former swing low above it is
resistance (role reversal): the side of price decides the kind, not which kind
of swing formed it.
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import aoi

from ._candles import BASE_TS, HOUR, candle


def _turn_low(ts: float, body_low: float) -> list[dict]:
    """Five bars whose middle one is a confirmed swing low with its body
    bottom at `body_low` and a 3-point wick beneath it."""
    b = body_low
    return [
        candle(ts + 0 * HOUR, b + 30, b + 32, b + 20, b + 22),
        candle(ts + 1 * HOUR, b + 22, b + 24, b + 8, b + 10),
        candle(ts + 2 * HOUR, b + 10, b + 12, b - 3, b),        # the swing low
        candle(ts + 3 * HOUR, b, b + 14, b - 1, b + 12),
        candle(ts + 4 * HOUR, b + 12, b + 26, b + 10, b + 24),
    ]


def _turn_high(ts: float, body_high: float) -> list[dict]:
    """Mirror of `_turn_low`: a swing high whose body top is `body_high`."""
    b = body_high
    return [
        candle(ts + 0 * HOUR, b - 30, b - 20, b - 32, b - 22),
        candle(ts + 1 * HOUR, b - 22, b - 8, b - 24, b - 10),
        candle(ts + 2 * HOUR, b - 10, b + 3, b - 12, b),        # the swing high
        candle(ts + 3 * HOUR, b, b + 1, b - 14, b - 12),
        candle(ts + 4 * HOUR, b - 12, b - 10, b - 26, b - 24),
    ]


def _chart(*blocks: list[dict]) -> list[dict]:
    """Concatenate blocks, re-stamping so time only moves forward."""
    out: list[dict] = []
    for block in blocks:
        for c in block:
            out.append({**c, "ts": BASE_TS + len(out) * HOUR})
    return out


class TestLevels:
    def test_three_turns_at_the_same_price_are_one_validated_level(self):
        chart = _chart(_turn_low(0, 4000.0), _turn_low(0, 4004.0),
                       _turn_low(0, 4002.0), _turn_high(0, 4300.0))

        # 0.5% (~$21): the joins between the fixture's blocks are real swing
        # highs ~32 points up, and at 0.8% they would join this level too.
        found = aoi.levels(chart, price=4150.0, width_pct=0.005, min_touches=3)

        demand = [z for z in found if z["kind"] == "demand"]
        assert len(demand) == 1, found
        assert demand[0]["touches"] == 3
        assert 3995.0 <= demand[0]["low"] <= 4000.0
        assert 4004.0 <= demand[0]["high"] <= 4010.0

    def test_three_turns_far_apart_are_not_a_level(self):
        """Scattered turns are a trend, not a level. The body-merge counted
        them as one band because their bodies overlapped."""
        chart = _chart(_turn_low(0, 4000.0), _turn_low(0, 4080.0),
                       _turn_low(0, 4160.0), _turn_high(0, 4400.0))

        found = aoi.levels(chart, price=4300.0, width_pct=0.008, min_touches=3)

        assert [z for z in found if z["kind"] == "demand"] == [], found

    def test_no_level_is_ever_wider_than_the_width_it_was_given(self):
        """The property whose absence emptied the page: a level wider than
        the cap is thrown away by `propose`, so building one is building
        nothing."""
        chart = _chart(*[_turn_low(0, 4000.0 + 7.0 * i) for i in range(8)],
                       _turn_high(0, 4400.0))
        price = 4300.0

        found = aoi.levels(chart, price=price, width_pct=0.008, min_touches=3)

        assert found, "eight turns 7 points apart must validate something"
        for z in found:
            assert z["high"] - z["low"] <= price * 0.008 + 1e-9, z

    def test_a_former_swing_high_below_price_is_demand(self):
        """Role reversal. Resistance that price has since broken above is
        where a long is taken on the retest."""
        chart = _chart(_turn_high(0, 4100.0), _turn_high(0, 4102.0),
                       _turn_high(0, 4101.0), _turn_low(0, 4150.0))

        found = aoi.levels(chart, price=4200.0, width_pct=0.008, min_touches=3)

        assert [z["kind"] for z in found] == ["demand"], found

    def test_a_level_from_one_exact_price_still_has_height(self):
        """Three turns to the tick would otherwise be a zero-height band that
        price can never be inside."""
        chart = _chart(_turn_low(0, 4000.0), _turn_low(0, 4000.0),
                       _turn_low(0, 4000.0), _turn_high(0, 4300.0))

        (z,) = [z for z in aoi.levels(chart, price=4150.0, width_pct=0.008,
                                      min_touches=3) if z["kind"] == "demand"]

        assert z["high"] > z["low"]
        assert z["low"] <= 4000.0 <= z["high"]

    def test_an_empty_series_is_no_levels(self):
        assert aoi.levels([], price=4000.0) == []


class TestMarkUsesLevels:
    def test_mark_can_build_from_levels_and_still_stops_at_the_pair(self):
        chart = _chart(_turn_low(0, 4000.0), _turn_low(0, 4003.0),
                       _turn_low(0, 4001.0),
                       _turn_high(0, 4300.0), _turn_high(0, 4302.0),
                       _turn_high(0, 4301.0),
                       # price drifts back to the middle
                       [candle(0, 4280, 4282, 4150, 4152)])

        zones, bars = aoi.mark(chart, 4152.0, cluster_width_pct=0.008)

        assert [z["kind"] for z in zones] == ["demand", "supply"], zones
        assert all(z["touches"] >= 3 for z in zones)
        assert bars <= len(chart)

    def test_without_the_argument_mark_is_unchanged(self):
        """Every existing caller and test of `mark` reads the body bands."""
        chart = _chart(_turn_low(0, 4000.0), _turn_low(0, 4003.0),
                       _turn_low(0, 4001.0), _turn_high(0, 4300.0))

        assert aoi.mark(chart, 4150.0) == aoi.mark(chart, 4150.0,
                                                    cluster_width_pct=None)


@pytest.mark.asyncio
async def test_gather_offers_a_usable_zone_on_a_chart_of_wide_gold_bodies():
    """The live failure, reproduced. Daily bodies ~40-60 points at ~$4,200:
    the body-merge built bands wider than the 1.5% cap and `propose` saw no
    usable zone at all. Levels drawn where price turned must survive it."""
    from backend.src.services.setforget import analysis

    def big_low(b):
        return [candle(0, b + 150, b + 155, b + 95, b + 100),
                candle(0, b + 100, b + 105, b + 45, b + 50),
                candle(0, b + 50, b + 55, b - 20, b),
                candle(0, b, b + 55, b - 5, b + 50),
                candle(0, b + 50, b + 105, b + 45, b + 100)]

    def big_high(b):
        return [candle(0, b - 150, b - 95, b - 155, b - 100),
                candle(0, b - 100, b - 45, b - 105, b - 50),
                candle(0, b - 50, b + 20, b - 55, b),
                candle(0, b, b + 5, b - 55, b - 50),
                candle(0, b - 50, b - 45, b - 105, b - 100)]

    # Three turns each side, 15 points apart: close enough to be one level,
    # but their 50-point bodies overlap into an 80-point band once merged.
    daily = _chart(big_low(4000.0), big_high(4400.0), big_low(4030.0),
                   big_high(4370.0), big_low(4015.0), big_high(4385.0),
                   [candle(0, 4300, 4305, 4195, 4200)])

    class _Engine:
        async def get_candles(self, timeframe, count=200):
            return daily if timeframe == "D1" else daily[-60:]

    ev = await analysis.gather(_Engine())
    usable = aoi.within_width(ev["zones"], ev["price"])

    assert {z["kind"] for z in usable} == {"demand", "supply"}, ev["zones"]
