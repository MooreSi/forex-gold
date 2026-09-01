"""The clock the Trading Schedule and the reports are read against.

Owner decision, 2026-09-01:

    "It should always be local time -- so if I'm based in the UK use my local
    time based on the time of the year, and if there are other users in other
    countries use their specific local time."

So: the user's own wall clock, wherever they are, with daylight saving handled.

**The operating system already knows that**, and a bare `datetime.now()`
returns exactly it -- which is what this code did originally. The original was
not wrong about the clock. It was wrong about one machine.

A **VPS is not where its user is.** On a paired install the Trading Schedule is
mirrored between the Mac and the VPS, so the setting travels and the clock does
not: a 09:00 window set in the UK gated a different part of the trading day on
a server abroad, and nothing reported the difference. The fix is not to
hardcode a country -- an install in Singapore has the same right to its own
clock -- it is to let a machine that is not where its user is be TOLD the
user's offset.

Hence two modes:

  * **no offset configured** (the default) -- the machine's own local time.
    Right for every ordinary single-machine install, in any country, daylight
    saving included, and it needs no timezone database.
  * **an offset configured** -- that offset from UTC instead, for a VPS that
    should follow its owner rather than its data centre.

No `tzdata` dependency either way: the OS supplies the local zone, and an
offset is arithmetic. The cost of that choice is that a configured offset is a
fixed number -- it does not follow the user's daylight saving on its own, and
has to be refreshed when their clocks change. See docs/simon-handover/020 for
the proposal to have the paired Mac report its offset automatically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

__all__ = [
    "SETTING_KEY", "machine_offset_minutes", "configured_offset_minutes",
    "local_now", "local_from", "local_from_timestamp", "local_timestamp",
]

SETTING_KEY = "trading_clock_offset_min"

# More than a day away from UTC is a typo, not a timezone. Real zones span
# -12:00 to +14:00; this is deliberately a little wider and still a sanity net.
# Public: the setter in services/risk/clock validates against this same
# ceiling, and a second copy of the number would drift from this one.
MAX_OFFSET_MIN = 24 * 60


def machine_offset_minutes() -> int:
    """This machine's current offset from UTC, daylight saving included."""
    return int(datetime.now().astimezone().utcoffset().total_seconds() // 60)


def configured_offset_minutes(rs: dict) -> Optional[int]:
    """The configured offset, or None meaning "use this machine's own clock".

    None on anything unusable rather than raising: this is read on the path
    that decides whether to trade, and falling back to the machine's clock is
    the behaviour every single-machine install wants anyway.

    Note 0 is a real answer (UTC), not absence -- `or` would get that wrong.
    """
    raw = rs.get(SETTING_KEY)
    if raw is None or raw == "":
        return None
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        log.warning("[clock] %s is not a number (%r) — using this machine's "
                    "own clock", SETTING_KEY, raw)
        return None
    if abs(offset) > MAX_OFFSET_MIN:
        log.warning("[clock] %s of %d minutes is not a timezone — using this "
                    "machine's own clock", SETTING_KEY, offset)
        return None
    return offset


def local_from(moment: datetime, offset_minutes: Optional[int] = None) -> datetime:
    """A UTC instant as naive local wall-clock time.

    Naive deliberately: every caller compares `.hour` and `.weekday()` against
    numbers a person typed into a settings screen, and against other naive
    datetimes. Handing back an aware one would change how those compare
    elsewhere for no benefit here.
    """
    utc = moment.astimezone(timezone.utc)
    if offset_minutes is None:
        return utc.astimezone().replace(tzinfo=None)
    return (utc + timedelta(minutes=offset_minutes)).replace(tzinfo=None)


def local_now(offset_minutes: Optional[int] = None) -> datetime:
    """Now, on the trading clock. The drop-in for a bare `datetime.now()`."""
    return local_from(datetime.now(timezone.utc), offset_minutes)


def local_from_timestamp(epoch: float,
                         offset_minutes: Optional[int] = None) -> datetime:
    """A stored epoch time on the trading clock.

    The drop-in for `datetime.fromtimestamp(x)`. That is already the machine's
    zone, so the default behaves identically -- the point is that it follows
    the CONFIGURED clock when there is one, so report buckets and schedule
    boundaries cannot disagree.
    """
    return local_from(datetime.fromtimestamp(epoch, tz=timezone.utc),
                      offset_minutes)


def local_timestamp(wall: datetime,
                    offset_minutes: Optional[int] = None) -> float:
    """The epoch second for a naive trading-clock time.

    The drop-in for `naive.timestamp()`. Used for day and week boundaries:
    local midnight is rarely 00:00 UTC.
    """
    if offset_minutes is None:
        return wall.timestamp()          # the machine's own zone, as before
    return (wall - timedelta(minutes=offset_minutes)
            ).replace(tzinfo=timezone.utc).timestamp()
