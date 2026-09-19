"""Areas of interest -- the zones the whole method is built around.

An AOI is where Alex G will trade and nowhere else is, so two failures matter
more than the rest:

**A zone that is too wide swallows the price.** Every candidate then looks like
it is "at a zone", the confluence checklist scores it, and the method has
quietly become "trade whenever the AI feels like it". The width comes from the
swing candle's own wick, which is why the derivation is asserted exactly rather
than approximately.

**The target is the next OPPOSING zone.** Taking profit at the next zone of
the same kind would mean a long targeting support -- price would have to fall
to reach it. That mistake reads as a plausible number on screen, which is
exactly why it gets its own test.
"""
from __future__ import annotations

from backend.src.services.setforget import aoi

from ._candles import candle, zigzag


def _zone(kind, low, high, ts=0.0, touches=1):
    return {"kind": kind, "low": low, "high": high, "ts": ts, "touches": touches}


class TestZones:
    def test_a_swing_low_becomes_a_demand_zone_from_the_wick_to_the_body(self):
        candles = zigzag([(120.0, 0), (100.0, 6), (130.0, 6)])
        demand = [z for z in aoi.zones(candles) if z["kind"] == "demand"]

        assert len(demand) == 1
        swing = candles[6]
        assert demand[0]["low"] == swing["low"]
        assert demand[0]["high"] == min(swing["open"], swing["close"])

    def test_a_swing_high_becomes_a_supply_zone(self):
        candles = zigzag([(100.0, 0), (130.0, 6), (105.0, 6)])
        supply = [z for z in aoi.zones(candles) if z["kind"] == "supply"]

        assert len(supply) == 1
        swing = candles[6]
        assert supply[0]["high"] == swing["high"]
        assert supply[0]["low"] == max(swing["open"], swing["close"])

    def test_a_zone_is_never_zero_height(self):
        """A swing candle with no wick on the rejecting side would otherwise
        produce a zone with low == high. Price is then never "in" it, the
        checklist never scores, and the section shows a zone nobody can trade
        while looking perfectly normal."""
        # Body from 100 to 105, and a low exactly at the body's bottom.
        candles = [candle(0, 110.0, 111.0, 106.0, 107.0),
                   candle(1, 108.0, 108.5, 104.0, 105.0),
                   candle(2, 100.0, 105.0, 100.0, 105.0),      # no lower wick
                   candle(3, 105.0, 109.0, 104.5, 108.0),
                   candle(4, 108.0, 112.0, 107.5, 111.0)]
        demand = [z for z in aoi.zones(candles) if z["kind"] == "demand"]

        assert demand, "the swing low should still produce a zone"
        assert demand[0]["high"] > demand[0]["low"]

    def test_a_supply_zone_with_no_upper_wick_falls_back_to_its_body(self):
        """The mirror of the case above. A swing high that closed on its high
        has no rejection wick, so the band would be zero-height -- price is
        never "in" it, the checklist never scores, and the page shows an
        untradeable level that looks entirely normal."""
        candles = [candle(0, 90.0, 91.0, 89.0, 90.5),
                   candle(1, 91.0, 92.0, 90.0, 91.5),
                   candle(2, 95.0, 100.0, 94.0, 100.0),      # no upper wick
                   candle(3, 99.0, 99.5, 96.0, 96.5),
                   candle(4, 96.0, 96.5, 92.0, 93.0)]
        supply = [z for z in aoi.zones(candles) if z["kind"] == "supply"]

        assert supply, "the swing high should still produce a zone"
        assert supply[0]["high"] > supply[0]["low"]
        assert supply[0]["low"] == 95.0                       # the body's open

    def test_a_candle_with_no_range_at_all_produces_no_zone(self):
        """A bar whose open, high, low and close are one price is a gap in the
        feed, not a level. A zero-height band from it would be drawn, counted
        as a touch and offered as an entry."""
        flat = candle(2, 100.0, 100.0, 100.0, 100.0)
        candles = [candle(0, 110.0, 111.0, 106.0, 107.0),
                   candle(1, 108.0, 108.5, 104.0, 105.0),
                   flat,
                   candle(3, 105.0, 109.0, 104.5, 108.0),
                   candle(4, 108.0, 112.0, 107.5, 111.0)]

        # It IS a confirmed swing low -- the negative control for the test.
        from backend.src.services.setforget import structure
        assert any(p["idx"] == 2 for p in structure.swing_points(candles))

        assert aoi.zones(candles) == []

    def test_overlapping_zones_of_the_same_kind_merge_into_one(self):
        """Two swing lows a tick apart are one area of interest, not two. Left
        separate they would each score the checklist and the same level would
        be counted twice."""
        merged = aoi.merge([
            _zone("demand", 100.0, 102.0, ts=1.0),
            _zone("demand", 101.5, 103.0, ts=2.0),
            _zone("demand", 120.0, 121.0, ts=3.0),
        ])

        assert len(merged) == 2
        assert merged[0]["low"] == 100.0
        assert merged[0]["high"] == 103.0
        assert merged[0]["touches"] == 2

    def test_zones_a_hair_apart_merge_even_though_they_do_not_overlap(self):
        """Found by looking at a real 400-bar window: the detector produced a
        dozen bands stacked within a few points of each other, so the "next
        opposing zone" was always a point or two from the entry and EVERY
        candidate came out under 1:2. A trader drawing this chart by hand draws
        four or five chunky levels, not twelve hairlines.

        The gap is the caller's, because what counts as "a hair" is how far the
        instrument moves in a bar, not a number this module should pick.
        """
        merged = aoi.merge([
            _zone("demand", 100.0, 101.0),
            _zone("demand", 102.0, 103.0),      # a 1.0 gap
        ], gap=1.5)

        assert len(merged) == 1
        assert (merged[0]["low"], merged[0]["high"]) == (100.0, 103.0)

    def test_a_gap_wider_than_the_tolerance_stays_two_zones(self):
        merged = aoi.merge([
            _zone("demand", 100.0, 101.0),
            _zone("demand", 102.0, 103.0),
        ], gap=0.5)

        assert len(merged) == 2

    def test_the_default_gap_is_zero_so_only_overlaps_merge(self):
        merged = aoi.merge([
            _zone("demand", 100.0, 101.0),
            _zone("demand", 102.0, 103.0),
        ])

        assert len(merged) == 2

    def test_merging_by_proximity_is_transitive_across_a_chain(self):
        """Three bands each within the gap of the next are one level, not two.
        A sweep that only compares each zone to the ORIGINAL previous one would
        leave the third out and put a target two points from the entry."""
        merged = aoi.merge([
            _zone("demand", 100.0, 101.0),
            _zone("demand", 102.0, 103.0),
            _zone("demand", 104.0, 105.0),
        ], gap=1.5)

        assert len(merged) == 1
        assert merged[0]["high"] == 105.0
        assert merged[0]["touches"] == 3

    def test_a_demand_zone_never_merges_into_a_supply_zone(self):
        merged = aoi.merge([
            _zone("demand", 100.0, 102.0),
            _zone("supply", 101.0, 103.0),
        ])

        assert len(merged) == 2

    def test_the_merged_zone_keeps_the_most_recent_timestamp(self):
        """It is one level, and the age shown beside it should be the last time
        price was there -- not the first."""
        merged = aoi.merge([
            _zone("demand", 100.0, 102.0, ts=10.0),
            _zone("demand", 101.0, 103.0, ts=90.0),
        ])

        assert merged[0]["ts"] == 90.0

    def test_zones_come_back_in_price_order(self):
        candles = zigzag([(100.0, 0), (130.0, 6), (105.0, 6), (140.0, 6), (115.0, 6)])
        lows = [z["low"] for z in aoi.zones(candles)]

        assert lows == sorted(lows)

    def test_too_short_a_series_is_no_zones_rather_than_an_error(self):
        assert aoi.zones([]) == []


class TestTheTrim:
    """Beyond a handful of zones the chart is a wall of boxes. Which ones
    survive is not cosmetic: the trim decides which levels the entry and the
    target can be built from, so keeping the wrong ones changes the trade.

    Exercised through `zones` on a real series rather than against a copy of
    the sort -- a test that reimplements the thing it is checking passes
    whatever the code does.
    """

    # Ten legs, alternating, each turn a confirmed swing well clear of its
    # neighbours. Ends low, so the newest zones are at the BOTTOM of the range
    # and "nearest" and "newest" pick different sets.
    LEGS = [(100.0, 0), (160.0, 6), (120.0, 6), (200.0, 6), (150.0, 6),
            (240.0, 6), (190.0, 6), (280.0, 6), (230.0, 6), (320.0, 6),
            (260.0, 6)]

    def test_the_series_really_does_produce_more_zones_than_the_limit(self):
        """The negative control. Without this the two tests below would pass
        on a series that never had anything to trim."""
        assert len(aoi.zones(zigzag(self.LEGS), limit=99)) > 3

    def test_over_the_limit_only_that_many_come_back(self):
        assert len(aoi.zones(zigzag(self.LEGS), limit=3)) == 3

    def test_the_ones_nearest_the_reference_survive(self):
        """Nearest, not newest and not strongest. A setup is built from levels
        price can actually reach; a zone 200 points away cannot be an entry or
        a target today however many times it has been tested."""
        candles = zigzag(self.LEGS)
        all_zones = aoi.zones(candles, limit=99)
        expected = sorted(
            sorted(all_zones, key=lambda z: aoi.distance(z, 150.0))[:3],
            key=lambda z: z["low"])

        kept = aoi.zones(candles, limit=3, reference=150.0)

        assert [z["low"] for z in kept] == [z["low"] for z in expected]
        assert all(aoi.distance(z, 150.0) <= 90.0 for z in kept)

    def test_the_reference_defaults_to_the_last_close(self):
        """A trim measured from the wrong price keeps the wrong end of the
        chart, and every zone on it is still a real level -- so the page looks
        entirely normal while the setup is built from the far side."""
        candles = zigzag(self.LEGS)
        last = candles[-1]["close"]

        assert ([z["low"] for z in aoi.zones(candles, limit=3)]
                == [z["low"] for z in aoi.zones(candles, limit=3, reference=last)])

    def test_what_survives_still_comes_back_in_price_order(self):
        kept = aoi.zones(zigzag(self.LEGS), limit=3, reference=150.0)

        assert [z["low"] for z in kept] == sorted(z["low"] for z in kept)

    def test_under_the_limit_nothing_is_dropped(self):
        candles = zigzag([(100.0, 0), (160.0, 6), (120.0, 6)])

        assert aoi.zones(candles, limit=8) == aoi.zones(candles, limit=99)


class TestAtPrice:
    def test_a_price_inside_a_zone_finds_it(self):
        zones = [_zone("demand", 100.0, 102.0), _zone("supply", 130.0, 132.0)]

        found = aoi.at_price(zones, 101.0, "demand")

        assert found is not None and found["low"] == 100.0

    def test_a_price_just_outside_is_still_at_the_zone_within_tolerance(self):
        """Price rarely touches a hand-drawn level to the tick. A tolerance of
        zero would mean the setup only ever exists on the bar that happens to
        print inside it."""
        zones = [_zone("demand", 100.0, 102.0)]

        assert aoi.at_price(zones, 102.4, "demand", tolerance=0.5) is not None
        assert aoi.at_price(zones, 102.6, "demand", tolerance=0.5) is None

    def test_the_wrong_kind_of_zone_is_not_returned(self):
        zones = [_zone("supply", 100.0, 102.0)]

        assert aoi.at_price(zones, 101.0, "demand") is None

    def test_the_nearest_of_two_candidates_wins(self):
        zones = [_zone("demand", 90.0, 99.0), _zone("demand", 100.0, 102.0)]

        found = aoi.at_price(zones, 101.0, "demand", tolerance=5.0)

        assert found is not None and found["low"] == 100.0


class TestNextOpposing:
    def test_a_buy_targets_the_nearest_supply_above(self):
        zones = [_zone("demand", 98.0, 100.0), _zone("supply", 110.0, 112.0),
                 _zone("supply", 130.0, 132.0)]

        target = aoi.next_opposing(zones, 101.0, "BUY")

        assert target is not None and target["low"] == 110.0

    def test_a_sell_targets_the_nearest_demand_below(self):
        zones = [_zone("demand", 80.0, 82.0), _zone("demand", 95.0, 97.0),
                 _zone("supply", 110.0, 112.0)]

        target = aoi.next_opposing(zones, 105.0, "SELL")

        assert target is not None and target["high"] == 97.0

    def test_a_zone_on_the_wrong_side_of_price_is_not_a_target(self):
        """A long cannot target supply that price has already passed. Returning
        it would put the take-profit below the entry and invert the trade."""
        zones = [_zone("supply", 90.0, 92.0)]

        assert aoi.next_opposing(zones, 105.0, "BUY") is None

    def test_no_opposing_zone_is_none(self):
        assert aoi.next_opposing([_zone("demand", 98.0, 100.0)], 101.0, "BUY") is None
