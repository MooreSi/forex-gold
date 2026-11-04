"""The broker's clock follows New York, so its offset from UTC changes twice a year.

Vantage's MT5 server puts midnight at 17:00 New York: UTC+3 while the US is on
daylight saving, UTC+2 otherwise. Every reader of deal times assumed UTC+3, so
after 1 November 2026 each MT5 time was read an hour early. On 3 November two
losses closed at 00:21 and 00:55 UTC were filed under 2 November, and the day
showed -$9.46 while the daily loss guard (local table, true epochs) counted
-$29.70 and refused the next order.

Pure arithmetic: nothing here reaches a broker. No real or demo order is
placed, closed or modified.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.src.utils import broker_clock as bc


def _utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


class TestTheOffset:
    @pytest.mark.parametrize("when, expected", [
        ("2026-07-21 12:00:00", 10800),   # US summer
        ("2026-11-03 00:21:53", 7200),    # the 3 November trade
        ("2026-01-15 12:00:00", 7200),    # US winter
        ("2026-03-08 06:59:59", 7200),    # 01:59:59 EST, a second before the spring change
        ("2026-03-08 07:00:00", 10800),   # 03:00 EDT
        ("2026-11-01 05:59:59", 10800),   # 01:59:59 EDT, a second before the autumn change
        ("2026-11-01 06:00:00", 7200),    # 01:00 EST
        ("2027-03-14 07:00:00", 10800),   # second Sunday of March 2027
        ("2027-11-07 06:00:00", 7200),    # first Sunday of November 2027
    ])
    def test_it_follows_us_daylight_saving(self, when, expected):
        assert bc.offset_at_utc(_utc(when)) == expected


class TestConversions:
    def test_a_winter_broker_stamp_comes_back_to_its_real_utc(self):
        """MT5 showed the 3 November loss closing at 02:21:53."""
        broker_ts = _utc("2026-11-03 02:21:53")
        assert bc.broker_to_utc(broker_ts) == _utc("2026-11-03 00:21:53")

    def test_a_summer_broker_stamp_still_loses_three_hours(self):
        broker_ts = _utc("2026-07-21 15:00:00")
        assert bc.broker_to_utc(broker_ts) == _utc("2026-07-21 12:00:00")

    @pytest.mark.parametrize("when", [
        "2026-07-21 12:00:00", "2026-11-03 00:21:53", "2026-03-08 07:00:00",
        "2026-11-01 07:00:00", "2026-11-01 07:30:00",
    ])
    def test_round_trip(self, when):
        assert bc.broker_to_utc(bc.utc_to_broker(_utc(when))) == _utc(when)

    def test_the_hour_read_twice_is_taken_as_summer(self):
        """Broker 08:00 on 1 November is both 05:00 UTC (EDT) and 06:00 UTC (EST)."""
        assert bc.broker_to_utc(_utc("2026-11-01 08:00:00")) == _utc("2026-11-01 05:00:00")


class TestTheDayATradeIsFiledUnder:
    def test_a_close_twenty_minutes_after_midnight_is_today(self):
        from backend.src.services.analytics import formatting
        broker_ts = _utc("2026-11-03 02:21:53")
        assert formatting.broker_ts_to_local_date(broker_ts, 0) == date(2026, 11, 3)

    def test_a_summer_close_is_filed_as_before(self):
        from backend.src.services.analytics import formatting
        broker_ts = _utc("2026-07-22 02:30:00")   # 23:30 UTC on the 21st
        assert formatting.broker_ts_to_local_date(broker_ts, 0) == date(2026, 7, 21)


class TestTheSessionOfAWinterClose:
    def test_seven_thirty_utc_is_london_not_asian(self):
        from backend.src.services.analytics import trade_table
        broker_ts = _utc("2026-11-03 09:30:00")   # 07:30 UTC
        assert trade_table._session(broker_ts) == "london"


class TestTodaysCutoffInWinter:
    def test_midnight_is_two_hours_ahead_in_broker_time(self):
        from backend.src.services.broker.mt5_performance import broker_day_cutoff
        now = datetime(2026, 11, 3, 14, 30, tzinfo=timezone.utc)
        assert broker_day_cutoff(offset_minutes=0, now=now) == _utc("2026-11-03 00:00:00") + 7200
