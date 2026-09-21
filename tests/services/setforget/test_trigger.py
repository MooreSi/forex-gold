"""The 30-minute trigger -- when to enter, once the higher timeframes have
already said where and which way.

This is the stage the app did not have, and its absence is what put a resting
order days of travel from price on 2026-09-20. The Weekly and the Daily pick
the zone; the 4H agrees or does not; and then nothing happened until price
arrived, because there was no third stage to wait for. So the app rested an
order at the zone instead and waited for the market to come to it.

Alex G's guide puts the entry on the lowest of three timeframes -- "a 4:1 or
8:1 ratio (e.g. Daily, 4H, 30-min)" -- and the community's Perfect Checklist
gives a whole 25% group to "2H, 1H, 30m", holding Shift of Structure (10%),
Engulfing Pattern (10%) and Round Psychological Level (5%).

**Two gates, both required.** Price must have ARRIVED at the zone, and the 30m
must then have REACTED. Either alone is the failure it replaces: arrival alone
is an order resting in front of a moving market, and a reaction anywhere on the
chart is a trade with no level behind it.
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import trigger

from ._candles import candle, series


def _zone(low, high, kind="demand"):
    return {"kind": kind, "low": low, "high": high, "ts": 0.0, "touches": 3}


# Falls, bounces to a swing high at 121, falls again, then closes back through
# it -- a shift of structure on the last bar.
TURNED_UP = [118, 112, 106, 120, 108, 102, 98, 104, 112, 126]


class TestArrival:
    def test_price_inside_the_zone_has_arrived(self):
        assert trigger.has_arrived(_zone(1975.0, 1985.0), 1980.0, tolerance=1.0)

    def test_price_within_tolerance_has_arrived(self):
        """Price rarely touches a hand-drawn level to the tick."""
        assert trigger.has_arrived(_zone(1975.0, 1985.0), 1985.8, tolerance=1.0)

    def test_price_nowhere_near_has_not(self):
        """The case that matters. This is the state the app used to place an
        order in, and it is the whole reason the stage exists."""
        assert not trigger.has_arrived(_zone(1975.0, 1985.0), 2400.0, tolerance=1.0)

    def test_no_zone_is_not_an_arrival(self):
        assert not trigger.has_arrived(None, 1980.0, tolerance=1.0)


class TestTheTrigger:
    def test_a_shift_of_structure_triggers(self):
        found = trigger.evaluate(series(TURNED_UP), "BUY")

        assert found is not None
        assert found["kind"] == "shift_of_structure"

    def test_an_engulfing_close_triggers(self):
        """The checklist's other 10% item in the same group. A shift is the
        stronger read, but an engulfing at the level is a trigger in its own
        right and the guide lists it first among the patterns."""
        flat = [candle(i * 1800.0, 100.0, 100.4, 99.6, 100.1) for i in range(6)]
        flat.append(candle(6 * 1800.0, 100.0, 100.5, 97.0, 97.5))   # down bar
        flat.append(candle(7 * 1800.0, 97.2, 101.5, 97.0, 101.0))   # engulfs it
        flat.append(candle(8 * 1800.0, 101.0, 101.2, 100.9, 101.1))  # forming

        found = trigger.evaluate(flat, "BUY")

        assert found is not None
        assert found["kind"] == "engulfing"

    def test_a_shift_outranks_an_engulfing_when_both_are_there(self):
        """They are 10% each on the checklist, but a shift is a statement about
        structure and an engulfing is one bar. Reporting the weaker of the two
        would understate what the chart did."""
        found = trigger.evaluate(series(TURNED_UP), "BUY")

        assert found is not None and found["kind"] == "shift_of_structure"

    def test_nothing_having_happened_is_none(self):
        """The common case, and it must be quiet. A trigger stage that fires on
        arrival is the stage it replaces, wearing a new name."""
        still_falling = series([118, 112, 106, 120, 108, 102, 98, 96, 94])

        assert trigger.evaluate(still_falling, "BUY") is None

    def test_a_trigger_the_wrong_way_does_not_count(self):
        """A bearish shift while waiting to buy is the zone failing, not the
        entry arriving."""
        assert trigger.evaluate(series(TURNED_UP), "SELL") is None

    def test_it_reports_when_it_fired(self):
        found = trigger.evaluate(series(TURNED_UP), "BUY")

        assert found is not None
        assert found["ts"] > 0

    def test_too_short_a_series_is_none_rather_than_an_error(self):
        assert trigger.evaluate([], "BUY") is None


class TestRoundLevel:
    """The checklist's 5% item in the same group: "Round Psychological Level".

    Gold trades around round hundreds and fifties, and those levels hold
    without any structure behind them because that is where people put orders.
    """

    @pytest.mark.parametrize("price", [4300.0, 4350.0, 4400.0])
    def test_a_round_number_is_found(self, price):
        assert trigger.round_level(price, tolerance=2.0) == pytest.approx(price)

    def test_a_price_near_a_round_number_still_finds_it(self):
        assert trigger.round_level(4348.6, tolerance=2.0) == pytest.approx(4350.0)

    def test_a_price_nowhere_near_one_finds_nothing(self):
        assert trigger.round_level(4372.0, tolerance=2.0) is None

    def test_the_tolerance_is_the_callers(self):
        assert trigger.round_level(4372.0, tolerance=30.0) == pytest.approx(4350.0)

    def test_it_rounds_to_the_nearer_of_the_two(self):
        assert trigger.round_level(4320.0, tolerance=30.0) == pytest.approx(4300.0)
        assert trigger.round_level(4340.0, tolerance=30.0) == pytest.approx(4350.0)
