"""UK wall-clock time, without a timezone database.

The Trading Schedule is set in UK time (owner decision,
docs/simon-handover/017) because that is the clock Simon keeps track of. It has
to mean the same instant on the Mac and on the VPS, so it cannot be "whatever
this machine's local time is" — the schedule is mirrored between the two and
the setting travels while the clock does not.

`zoneinfo` would be the obvious tool, but on Windows it needs the `tzdata`
package, which this project does not depend on and which is not worth adding
for one fixed zone. The UK rules are simple and have been stable since 2002:

    BST (UTC+1) from 01:00 UTC on the last Sunday in March
    GMT (UTC+0) from 01:00 UTC on the last Sunday in October

The cross-check below runs the implementation against `zoneinfo` on every
machine that has a timezone database, which is how a rule change — or a bug in
the arithmetic — would be caught. It skips where `tzdata` is absent rather than
failing, because its absence is the reason this module exists.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.utils import uk_clock


def _utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestTheOffsetAtTheBoundaries:
    """The two instants a year when getting this wrong is a whole hour."""

    @pytest.mark.parametrize("moment,hours", [
        (_utc(2026, 3, 29, 0, 59), 0),   # one minute before the spring change
        (_utc(2026, 3, 29, 1, 0), 1),    # the change itself
        (_utc(2026, 3, 29, 1, 1), 1),
        (_utc(2026, 10, 25, 0, 59), 1),  # one minute before the autumn change
        (_utc(2026, 10, 25, 1, 0), 0),   # back to GMT
    ])
    def test_2026(self, moment, hours):
        assert uk_clock.utc_offset_hours(moment) == hours

    @pytest.mark.parametrize("year,march,october", [
        (2024, 31, 27), (2025, 30, 26), (2026, 29, 25),
        (2027, 28, 31), (2028, 26, 29),
    ])
    def test_the_last_sunday_is_found_correctly(self, year, march, october):
        assert uk_clock.last_sunday(year, 3).day == march
        assert uk_clock.last_sunday(year, 10).day == october

    def test_midwinter_is_gmt(self):
        assert uk_clock.utc_offset_hours(_utc(2026, 1, 15, 12)) == 0

    def test_midsummer_is_bst(self):
        assert uk_clock.utc_offset_hours(_utc(2026, 7, 15, 12)) == 1

    def test_a_march_sunday_that_is_not_the_LAST_one(self, ):
        """22 March 2026 is a Sunday, but not the last. Matching on "a Sunday
        in March" rather than the last one would switch a week early."""
        assert _utc(2026, 3, 22).weekday() == 6
        assert uk_clock.utc_offset_hours(_utc(2026, 3, 22, 12)) == 0


class TestWallClockTime:
    def test_it_returns_naive_uk_wall_time(self):
        """Naive on purpose: every caller compares `.hour` and `.weekday()`
        against numbers the operator typed, and an aware datetime would change
        how those compare elsewhere."""
        got = uk_clock.uk_wall_time(_utc(2026, 7, 15, 12, 30))

        assert got == datetime(2026, 7, 15, 13, 30)
        assert got.tzinfo is None

    def test_in_winter_it_matches_utc(self):
        assert uk_clock.uk_wall_time(_utc(2026, 1, 15, 12, 30)) == \
            datetime(2026, 1, 15, 12, 30)

    def test_it_can_cross_a_date_boundary(self):
        """23:30 UTC in summer is 00:30 the next day in the UK — so 'today' for
        the schedule is not always 'today' in UTC."""
        assert uk_clock.uk_wall_time(_utc(2026, 7, 15, 23, 30)) == \
            datetime(2026, 7, 16, 0, 30)

    def test_now_is_close_to_the_real_thing(self):
        a = uk_clock.uk_now()
        b = uk_clock.uk_wall_time(datetime.now(timezone.utc))

        assert abs((a - b).total_seconds()) < 5


_HAVE_TZDB = True
try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover - platform dependent
    _HAVE_TZDB = False


@pytest.mark.skipif(not _HAVE_TZDB,
                    reason="no system timezone database (this is why the "
                           "module exists); the check runs where there is one")
class TestItAgreesWithTheRealTimezoneDatabase:
    """The real verification. If the UK ever changes its DST rules, or the
    arithmetic above is wrong, this is what says so — on any machine with a
    timezone database, which includes CI on macOS."""

    def test_every_hour_of_four_years(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        moment = _utc(2024, 1, 1)
        end = _utc(2028, 1, 1)
        step = timedelta(hours=1)
        mismatches = []
        while moment < end:
            theirs = moment.astimezone(london).replace(tzinfo=None)
            ours = uk_clock.uk_wall_time(moment)
            if ours != theirs:
                mismatches.append((moment.isoformat(), ours.isoformat(),
                                   theirs.isoformat()))
                if len(mismatches) > 5:
                    break
            moment += step

        assert mismatches == [], (
            f"disagrees with the timezone database at {len(mismatches)} "
            f"instant(s), first few: {mismatches[:3]}"
        )

    def test_the_transition_minutes_match_exactly(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        for y in (2024, 2025, 2026, 2027, 2028):
            for month in (3, 10):
                change = uk_clock.last_sunday(y, month)
                base = _utc(y, month, change.day, 1, 0)
                for delta in (-timedelta(minutes=1), timedelta(0),
                              timedelta(minutes=1)):
                    moment = base + delta
                    assert uk_clock.uk_wall_time(moment) == \
                        moment.astimezone(london).replace(tzinfo=None), moment


class TestConvertingToAndFromStoredTimestamps:
    """The balance report buckets trades into UK days, and trade close times
    are stored as epoch seconds. Both directions of that conversion have to go
    through the UK clock, or the buckets are the machine's days wearing UK
    labels.

    `datetime.fromtimestamp(x)` with no timezone and `naive.timestamp()` both
    silently use the machine's local zone, which is exactly the bug being
    fixed.
    """

    def test_a_stored_time_becomes_uk_wall_time(self):
        epoch = _utc(2026, 7, 15, 23, 30).timestamp()

        assert uk_clock.uk_from_timestamp(epoch) == datetime(2026, 7, 16, 0, 30)

    def test_and_back_again(self):
        epoch = _utc(2026, 7, 15, 23, 30).timestamp()

        wall = uk_clock.uk_from_timestamp(epoch)

        assert uk_clock.uk_timestamp(wall) == pytest.approx(epoch)

    @pytest.mark.parametrize("moment", [
        _utc(2026, 1, 15, 12), _utc(2026, 3, 29, 0, 59), _utc(2026, 3, 29, 1, 1),
        _utc(2026, 7, 15, 23, 30), _utc(2026, 10, 25, 0, 59), _utc(2026, 12, 31, 23, 59),
    ])
    def test_it_round_trips_across_the_year(self, moment):
        epoch = moment.timestamp()

        assert uk_clock.uk_timestamp(
            uk_clock.uk_from_timestamp(epoch)) == pytest.approx(epoch)

    def test_a_uk_midnight_is_the_right_instant(self):
        """The day boundary the report buckets on. In summer UK midnight is
        23:00 UTC the day before, not 00:00 UTC."""
        summer_midnight = datetime(2026, 7, 16, 0, 0)

        assert uk_clock.uk_timestamp(summer_midnight) == \
            _utc(2026, 7, 15, 23, 0).timestamp()

    def test_a_winter_midnight_matches_utc(self):
        assert uk_clock.uk_timestamp(datetime(2026, 1, 16, 0, 0)) == \
            _utc(2026, 1, 16, 0, 0).timestamp()

    def test_the_repeated_autumn_hour_resolves_deterministically(self):
        """01:30 UK happens twice on the October change day. Whichever is
        chosen, it must be the same every time — a day boundary that moved
        between two calls would put the same trade in two different buckets."""
        ambiguous = datetime(2026, 10, 25, 1, 30)

        assert uk_clock.uk_timestamp(ambiguous) == uk_clock.uk_timestamp(ambiguous)
