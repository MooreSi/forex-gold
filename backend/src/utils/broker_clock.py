"""The broker's clock: New York 17:00 is its midnight.

MT5 stamps every deal, tick and bar in broker time written as if it were a UTC
epoch. The broker keeps New York's close at 00:00, so it runs at UTC+3 while
the US is on daylight saving and UTC+2 otherwise, and moves on the US dates,
not the European ones (`market/bar_clock.py`, measured 2026-10-17).

The trade-history readers assumed UTC+3 all year. From 1 November 2026 that
read every MT5 time an hour early: on 3 November two losses closed at 00:21
and 00:55 UTC were filed under the 2nd, and the day showed -$9.46 against the
-$29.70 the daily loss guard counted from its own table.

The US rule is written out rather than read from `zoneinfo`, which needs the
`tzdata` package on Windows and this project does not ship it. Since 2007:
second Sunday of March at 02:00 local to first Sunday of November at 02:00.

Pure. Imports nothing from the app.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["offset_at_utc", "broker_to_utc", "utc_to_broker", "SUMMER_S", "WINTER_S"]

SUMMER_S = 3 * 3600
WINTER_S = 2 * 3600


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _us_dst_bounds_utc(year: int) -> tuple[float, float]:
    """(start, end) of US daylight saving in `year`, as UTC epochs: 02:00 EST
    is 07:00 UTC, 02:00 EDT is 06:00 UTC."""
    start = _nth_sunday(year, 3, 2) + timedelta(hours=7)
    end = _nth_sunday(year, 11, 1) + timedelta(hours=6)
    return start.timestamp(), end.timestamp()


def offset_at_utc(utc_epoch: float) -> int:
    """Seconds the broker clock runs ahead of UTC at this real instant."""
    year = datetime.fromtimestamp(float(utc_epoch), tz=timezone.utc).year
    start, end = _us_dst_bounds_utc(year)
    return SUMMER_S if start <= float(utc_epoch) < end else WINTER_S


def utc_to_broker(utc_epoch: float) -> float:
    return float(utc_epoch) + offset_at_utc(utc_epoch)


def broker_to_utc(broker_ts: float) -> float:
    """The real UTC epoch of a broker-stamped time.

    The hour the clocks go back reads twice on the broker clock; it is taken
    as the summer reading."""
    as_summer = float(broker_ts) - SUMMER_S
    if offset_at_utc(as_summer) == SUMMER_S:
        return as_summer
    return float(broker_ts) - WINTER_S
