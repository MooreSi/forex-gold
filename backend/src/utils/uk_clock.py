"""UK wall-clock time, without a timezone database.

The Trading Schedule is set and read in UK time (owner decision,
docs/simon-handover/017): that is the clock its owner keeps track of, and the
windows were built around his day. It cannot be "whatever this machine's local
time is", because the schedule is mirrored between the Mac and the VPS by the
sync link — the setting travels, the clock does not. A 09:00 window set on the
Mac has to mean 09:00 UK on the VPS too, wherever the VPS happens to be.

`zoneinfo` is the obvious tool and is deliberately not used. On Windows it has
no system timezone database and needs the `tzdata` package, which this project
does not depend on; adding a dependency for one fixed zone is not worth it, and
Windows is the platform that actually runs this app.

The rules, unchanged since 2002 and set by statute:

    BST (UTC+1)  from 01:00 UTC on the last Sunday in March
    GMT (UTC+0)  from 01:00 UTC on the last Sunday in October

`tests/utils/test_uk_clock.py` cross-checks every hour of a four-year span
against `zoneinfo` on any machine that has a timezone database. That is what
would catch a rule change, or an error in the arithmetic here.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

__all__ = ["last_sunday", "utc_offset_hours", "uk_wall_time", "uk_now"]


def last_sunday(year: int, month: int) -> date:
    """The last Sunday of a month — the day both UK clock changes fall on."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    # weekday(): Monday is 0, Sunday is 6.
    return d - timedelta(days=(d.weekday() + 1) % 7)


def utc_offset_hours(moment: datetime) -> int:
    """0 for GMT, 1 for BST, for a timezone-aware UTC instant.

    The comparison is done in UTC on purpose. Both changes happen at 01:00 UTC,
    which is the one moment in the year when local time is ambiguous — in
    autumn 01:30 local happens twice — so anchoring on local wall time is
    exactly the wrong choice.
    """
    if moment.tzinfo is None:
        raise ValueError("utc_offset_hours needs a timezone-aware instant")
    utc = moment.astimezone(timezone.utc)
    y = utc.year
    spring = datetime(y, 3, last_sunday(y, 3).day, 1, 0, tzinfo=timezone.utc)
    autumn = datetime(y, 10, last_sunday(y, 10).day, 1, 0, tzinfo=timezone.utc)
    return 1 if spring <= utc < autumn else 0


def uk_wall_time(moment: datetime) -> datetime:
    """UK wall-clock time for a UTC instant, as a NAIVE datetime.

    Naive deliberately. Every caller compares `.hour` and `.weekday()` against
    numbers a person typed into a settings screen, and against other naive
    datetimes; handing back an aware one would change how those compare
    elsewhere for no benefit here.
    """
    utc = moment.astimezone(timezone.utc)
    return (utc + timedelta(hours=utc_offset_hours(utc))).replace(tzinfo=None)


def uk_now() -> datetime:
    """Now, as UK wall-clock time. The drop-in for a bare `datetime.now()`."""
    return uk_wall_time(datetime.now(timezone.utc))
