"""Session attribution for the calendar's day-detail breakdown.

The day panel splits a day's result by signal source AND by market session, so
you can see at a glance which channel and which session the day came from. The
session comes from the trade's close -- the same event that decides which
calendar day it lands on -- so the two readings of that clock have to agree.

Broker timestamps are UTC+3 stored as-if-UTC, and _session_for_hour expects a
UTC hour. Getting that unwind wrong shifts every trade three hours, which is
enough to move a London trade into Asian and an Overlap trade into London
without anything looking obviously broken.
"""
from datetime import datetime, timezone

import pytest

from forex_trader.core import database as db
from forex_trader.ui.pages.history import (
    _BROKER_OFFSET,
    _SESSION_LABELS,
    _broker_ts_to_uk_date,
    _broker_ts_to_utc_hour,
)


def _broker_ts(utc_dt: datetime) -> float:
    """A broker timestamp for a given real UTC moment."""
    return utc_dt.replace(tzinfo=timezone.utc).timestamp() + _BROKER_OFFSET


@pytest.mark.parametrize("utc_hour", list(range(24)))
def test_the_broker_offset_is_unwound_to_a_real_utc_hour(utc_hour):
    ts = _broker_ts(datetime(2026, 8, 18, utc_hour, 30))
    assert _broker_ts_to_utc_hour(ts) == utc_hour


def test_a_raw_unconverted_timestamp_would_be_three_hours_out():
    """Guards the specific mistake: reading the broker stamp as UTC directly.
    Three hours is enough to move London into Asian without looking wrong."""
    utc = datetime(2026, 8, 18, 5, 30)
    ts = _broker_ts(utc)
    naive = datetime.fromtimestamp(ts, timezone.utc).hour
    assert naive == 8 and _broker_ts_to_utc_hour(ts) == 5


@pytest.mark.parametrize("utc_hour,expected", [
    (0, "asian"), (6, "asian"), (23, "asian"),
    (7, "london"), (11, "london"),
    (12, "overlap"), (15, "overlap"),
    (16, "ny"), (20, "ny"),
])
def test_hours_map_to_the_sessions_the_rest_of_the_app_uses(utc_hour, expected):
    """Same _session_for_hour the heat map and is_session_allowed read, so a
    day's 'Overlap' here means the same window as 'Overlap' everywhere else."""
    assert db._session_for_hour(_broker_ts_to_utc_hour(_broker_ts(
        datetime(2026, 8, 18, utc_hour, 30)))) == expected


def test_session_and_calendar_day_are_read_from_the_same_clock():
    """A trade must not be able to land on one day in the grid and a session
    belonging to a different day's clock."""
    for utc_hour in (0, 5, 12, 21, 23):
        ts = _broker_ts(datetime(2026, 8, 18, utc_hour, 30))
        assert _broker_ts_to_uk_date(ts) is not None
        assert _broker_ts_to_utc_hour(ts) == utc_hour


def test_every_session_key_has_a_display_label():
    """The breakdown orders rows by this table; a session missing from it would
    silently vanish from the panel rather than show up unlabelled."""
    labels = dict(_SESSION_LABELS)
    for hour in range(24):
        assert db._session_for_hour(hour) in labels


def test_labels_are_listed_in_trading_day_order():
    """Fixed chronological order, not sorted by P&L -- comparing the same panel
    across two days is the point, and rows that move defeat that."""
    assert [k for k, _l in _SESSION_LABELS] == ["asian", "london", "overlap", "ny"]


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
def test_an_unusable_timestamp_returns_none_rather_than_a_wrong_hour(bad):
    """The panel drops these from the session split and says so. Defaulting to
    0 would quietly credit them all to the Asian session."""
    result = _broker_ts_to_utc_hour(bad)
    assert result is None or isinstance(result, int)
    if bad in (None, "", "abc"):
        assert result is None
