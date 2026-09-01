"""The clock the schedule and the reports are read against.

Owner decision, 2026-09-01:

> "It should always be local time — so if I'm based in the UK use my local time
> based on the time of the year, and if there are other users in other
> countries use their specific local time."

So: **the user's own wall clock, wherever they are, with daylight saving
handled.** The operating system already knows that, and `datetime.now()`
returns exactly it — which is what the code did originally.

The original was not wrong about the clock. It was wrong about one machine.
A **VPS is not where the user is.** On a paired install the schedule is
mirrored between the Mac and the VPS, so the setting travels while the clock
does not, and a 09:00 window set in the UK gated a different part of the day on
a server abroad. The fix for that is not to hardcode a country — an install in
Singapore has the same right to its own clock — it is to let a machine that is
not where the user is be told the user's offset.

Hence two modes:

  * **no offset configured** (the default) — the machine's own local time. Right
    for every ordinary single-machine install, in any country, DST included,
    and needs no timezone database.
  * **an offset configured** — that offset from UTC instead. For the VPS, so it
    follows its owner rather than its data centre.

No `tzdata` dependency either way: the OS supplies the local zone, and an
offset is arithmetic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.src.utils import trading_clock as tc


class TestTheDefaultIsTheMachinesOwnClock:
    """Which is the user's clock, on the user's machine — the ordinary case."""

    def test_now_matches_the_systems_local_time(self):
        got = tc.local_now(offset_minutes=None)
        expected = datetime.now()

        assert abs((got - expected).total_seconds()) < 5
        assert got.tzinfo is None, (
            "naive on purpose: every caller compares .hour and .weekday() "
            "against numbers a person typed into a settings screen"
        )

    def test_it_follows_the_machines_daylight_saving(self):
        """Not asserted by arithmetic — by agreeing with the OS, which is the
        only thing that knows this machine's rules."""
        offset = tc.machine_offset_minutes()
        expected = datetime.now(timezone.utc) + timedelta(minutes=offset)

        assert abs((tc.local_now() - expected.replace(tzinfo=None)
                    ).total_seconds()) < 5

    def test_the_machine_offset_is_a_whole_number_of_minutes(self):
        assert isinstance(tc.machine_offset_minutes(), int)


class TestAConfiguredOffsetOverridesIt:
    """For a machine that is not where its user is."""

    @pytest.mark.parametrize("offset,expected_hour", [
        (0, 12), (60, 13), (-300, 7), (480, 20), (330, 17),
    ])
    def test_the_offset_is_applied_to_utc(self, offset, expected_hour):
        instant = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        assert tc.local_from(instant, offset_minutes=offset).hour == expected_hour

    def test_it_ignores_the_machines_own_zone_entirely(self):
        """The whole point: the answer must not depend on where the server is."""
        instant = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        assert tc.local_from(instant, offset_minutes=60) == datetime(2026, 7, 15, 13, 0)

    def test_a_half_hour_zone_works(self):
        """India is +5:30, Nepal +5:45. An hours-only setting would exclude
        them, and 'other users in other countries' includes those."""
        instant = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        assert tc.local_from(instant, offset_minutes=345) == \
            datetime(2026, 7, 15, 17, 45)

    def test_it_can_cross_a_date_boundary(self):
        instant = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)

        assert tc.local_from(instant, offset_minutes=60).day == 16


class TestStoredTimestampsUseTheSameClock:
    """The reports bucket trades into days, and close times are stored as epoch
    seconds. Both directions have to agree with the clock the boundaries use,
    or the buckets are one machine's days wearing another's labels."""

    def test_a_stored_time_becomes_local_wall_time(self):
        epoch = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc).timestamp()

        assert tc.local_from_timestamp(epoch, offset_minutes=60) == \
            datetime(2026, 7, 16, 0, 30)

    def test_and_back_again(self):
        epoch = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc).timestamp()

        wall = tc.local_from_timestamp(epoch, offset_minutes=60)

        assert tc.local_timestamp(wall, offset_minutes=60) == pytest.approx(epoch)

    @pytest.mark.parametrize("offset", [0, 60, -300, 330, 720, -660])
    def test_it_round_trips_at_every_offset(self, offset):
        epoch = datetime(2026, 3, 14, 6, 45, tzinfo=timezone.utc).timestamp()

        assert tc.local_timestamp(
            tc.local_from_timestamp(epoch, offset_minutes=offset),
            offset_minutes=offset) == pytest.approx(epoch)

    def test_midnight_is_the_right_instant(self):
        """The day boundary the reports bucket on."""
        assert tc.local_timestamp(datetime(2026, 7, 16, 0, 0), offset_minutes=60) == \
            datetime(2026, 7, 15, 23, 0, tzinfo=timezone.utc).timestamp()

    def test_the_default_round_trips_against_the_machine_clock(self):
        epoch = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).timestamp()

        assert tc.local_timestamp(
            tc.local_from_timestamp(epoch)) == pytest.approx(epoch)


class TestReadingTheSetting:
    def test_no_setting_means_the_machines_own_clock(self):
        assert tc.configured_offset_minutes({}) is None

    def test_an_empty_value_means_the_same(self):
        """A blank field in the UI must not be read as UTC+0."""
        assert tc.configured_offset_minutes({"trading_clock_offset_min": ""}) is None
        assert tc.configured_offset_minutes({"trading_clock_offset_min": None}) is None

    def test_zero_is_a_real_offset_and_not_absence(self):
        """UTC+0 is a legitimate choice; it must not fall back to the machine."""
        assert tc.configured_offset_minutes({"trading_clock_offset_min": 0}) == 0

    def test_a_configured_offset_is_returned(self):
        assert tc.configured_offset_minutes(
            {"trading_clock_offset_min": -300}) == -300

    def test_nonsense_falls_back_to_the_machine_rather_than_raising(self):
        """This is read on the path that decides whether to trade."""
        assert tc.configured_offset_minutes(
            {"trading_clock_offset_min": "abc"}) is None

    @pytest.mark.parametrize("bad", [1441, -1441, 99999])
    def test_an_impossible_offset_is_refused(self, bad):
        """More than a day from UTC is a typo, not a timezone."""
        assert tc.configured_offset_minutes(
            {"trading_clock_offset_min": bad}) is None
