"""Weekly candles, built from daily ones.

There is no W1 anywhere in this app: `mt5_bridge._TF_MAP` stops at D1 and the
fake market's `TF_SECONDS` stops there too. Set & Forget's top-down read starts
at the weekly, so the choice was to teach the bridge a new timeframe -- a
change to the process that talks to the broker, for a read-only feature -- or
to aggregate the dailies it already serves. This is the aggregation.

The one thing that is easy to get wrong and impossible to see: **which week a
Sunday candle belongs to.** Gold's week opens Sunday evening UTC, and MT5
stamps its bars as UTC+3 encoded as if they were UTC (the same offset
`format.ts` exists to undo). Bucket on the raw stamp with an ISO week and the
Sunday open lands in the week that has just ENDED -- so the newest weekly bar
is built from one candle, the bias is read off a bar that is mostly noise, and
nothing on the chart looks wrong.
"""
from __future__ import annotations

import calendar
import datetime as dt

from backend.src.services.setforget import resample

from ._candles import candle

# MT5 encodes server time (UTC+3) as if it were a UTC epoch. The same constant
# as `format.ts`'s MT5_UTC_OFFSET_SECONDS, restated here so a test failure
# points at the offset rather than at an import.
MT5_OFFSET = 3 * 3600


def _stamp(y: int, m: int, d: int, hour: int = 0) -> float:
    """An MT5-style stamp for a real UTC instant."""
    real = calendar.timegm(dt.datetime(y, m, d, hour).timetuple())
    return float(real + MT5_OFFSET)


class TestWeekIndex:
    def test_a_week_turns_over_at_sunday_midnight_utc(self):
        # 2026-01-04 is a Sunday.
        saturday = resample.week_index(_stamp(2026, 1, 3, 23))
        sunday = resample.week_index(_stamp(2026, 1, 4, 0))

        assert sunday == saturday + 1

    def test_the_sunday_evening_open_belongs_to_the_week_that_is_starting(self):
        """The failure this module exists to prevent. Gold opens around 22:00
        UTC on Sunday; that bar is the first of the new week, not the last of
        the old one."""
        sunday_open = resample.week_index(_stamp(2026, 1, 4, 22))
        monday = resample.week_index(_stamp(2026, 1, 5, 12))

        assert sunday_open == monday

    def test_the_friday_close_belongs_to_the_week_that_is_ending(self):
        friday = resample.week_index(_stamp(2026, 1, 9, 21))
        monday = resample.week_index(_stamp(2026, 1, 5, 12))

        assert friday == monday

    def test_consecutive_weeks_are_consecutive_integers(self):
        first = resample.week_index(_stamp(2026, 1, 5))
        second = resample.week_index(_stamp(2026, 1, 12))

        assert second == first + 1


class TestToWeekly:
    def _week(self, monday_day: int, prices: list[tuple[float, float, float, float]]):
        return [candle(_stamp(2026, 1, monday_day + i), *p) for i, p in enumerate(prices)]

    def test_a_weekly_bar_takes_the_first_open_and_the_last_close(self):
        daily = self._week(5, [
            (100.0, 105.0, 99.0, 104.0),
            (104.0, 108.0, 103.0, 107.0),
            (107.0, 110.0, 101.0, 102.0),
        ])

        weekly = resample.to_weekly(daily)

        assert len(weekly) == 1
        assert weekly[0]["open"] == 100.0
        assert weekly[0]["close"] == 102.0

    def test_a_weekly_bar_takes_the_extreme_high_and_low_of_the_week(self):
        daily = self._week(5, [
            (100.0, 105.0, 99.0, 104.0),
            (104.0, 112.0, 103.0, 107.0),
            (107.0, 110.0, 95.0, 102.0),
        ])

        weekly = resample.to_weekly(daily)

        assert weekly[0]["high"] == 112.0
        assert weekly[0]["low"] == 95.0

    def test_the_weekly_bar_is_stamped_with_its_first_day(self):
        """A bar stamped with its LAST day plots a week into the future, which
        on a chart beside the daily reads as the weekly leading price."""
        daily = self._week(5, [(100.0, 105.0, 99.0, 104.0), (104.0, 108.0, 103.0, 107.0)])

        assert resample.to_weekly(daily)[0]["ts"] == daily[0]["ts"]

    def test_separate_weeks_stay_separate_and_come_back_in_order(self):
        daily = self._week(5, [(100.0, 105.0, 99.0, 104.0)]) \
            + self._week(12, [(104.0, 120.0, 104.0, 118.0)])

        weekly = resample.to_weekly(daily)

        assert len(weekly) == 2
        assert weekly[0]["ts"] < weekly[1]["ts"]
        assert weekly[1]["close"] == 118.0

    def test_out_of_order_input_is_sorted_rather_than_trusted(self):
        """The bridge returns oldest-first today. A weekly bar whose open came
        from whichever daily happened to be listed first would be wrong in a
        way that only shows up the day the bridge changes its mind."""
        early = candle(_stamp(2026, 1, 5), 100.0, 105.0, 99.0, 104.0)
        late = candle(_stamp(2026, 1, 8), 104.0, 108.0, 103.0, 107.0)

        weekly = resample.to_weekly([late, early])

        assert weekly[0]["open"] == 100.0
        assert weekly[0]["close"] == 107.0

    def test_an_empty_series_is_an_empty_series(self):
        assert resample.to_weekly([]) == []
