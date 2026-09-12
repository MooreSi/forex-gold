"""The day's lead figures must come from the day, not from the last four hours.

`_correlate` runs on a rolling 4-hour window and upserts one row per day. The
window aging out is already known to corrupt that row -- `re_correlated` was
being overwritten to 0 once the day's correlations were older than four hours,
and the fix was to re-count it from the database. That comment is still in
`reversal_engine_correlate.py`.

**Two fields were left behind.** `ref_predicted` -- how many of today's matches
this engine fired FIRST, which is the engine's entire purpose -- and
`avg_lead_time_s` are still computed from the window-local loop and upserted
over the day's real values. In the live database they are `0` and `NULL` on
**all 51 days on record**. The metric that says whether the engine leads or
lags the channel it exists to predict has never recorded a single value.

These tests are on the repo read that replaces them, which answers from the
day's own rows and so cannot be aged out.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_re_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_db.init(path)
    yield re_db
    re_db.close_db()
    remove_db_file(path)


def _correlated(db, *, created_at: float, delta: float | None, confirmed: int = 1):
    """One signal row that has been through the correlator."""
    sig_id = db.create_signal({
        "direction": "BUY", "entry_low": 4000.0, "entry_high": 4002.0,
        "stop_loss": 3995.0, "created_at": created_at,
    })
    if confirmed:
        db.update_correlation(sig_id=sig_id, ref_signal_id="ref-1",
                              time_delta_s=delta, distance_pts=1.0)
    return sig_id


class TestWhoFiredFirst:
    def test_a_negative_delta_means_we_led(self, fresh_re_db, monkeypatch):
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now, delta=-120.0)
        _correlated(fresh_re_db, created_at=now, delta=-30.0)

        led, _ = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 2

    def test_a_positive_delta_means_we_lagged_and_is_not_counted(self, fresh_re_db, monkeypatch):
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now, delta=-120.0)
        _correlated(fresh_re_db, created_at=now, delta=45.0)

        led, _ = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 1

    def test_the_mean_lead_is_signed(self, fresh_re_db, monkeypatch):
        """Negative means ahead. A mean that took absolute values would report
        an engine that is 100s early and 100s late as 100s early."""
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now, delta=-100.0)
        _correlated(fresh_re_db, created_at=now, delta=60.0)

        _, mean = fresh_re_db.today_lead_stats(now_ts=now)

        assert mean == pytest.approx(-20.0)


class TestItIsNotAgedOutByTheWindow:
    def test_a_correlation_from_this_morning_still_counts_this_evening(self, fresh_re_db, monkeypatch):
        """The whole point. The correlator's own window is four hours; the row
        it writes is for the day."""
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now + 60, delta=-90.0)

        led, mean = fresh_re_db.today_lead_stats(now_ts=now + 15 * 3600)

        assert led == 1
        assert mean == pytest.approx(-90.0)

    def test_yesterdays_correlations_are_not_counted_today(self, fresh_re_db, monkeypatch):
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now - 86400, delta=-90.0)

        led, mean = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 0
        assert mean is None


class TestWhenThereIsNothingToSay:
    def test_no_correlations_today_gives_no_mean_rather_than_zero(self, fresh_re_db, monkeypatch):
        """None means "no measurement". Zero would read as "dead level with the
        channel", which is a finding, and it would not be true."""
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now, delta=None, confirmed=0)

        led, mean = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 0
        assert mean is None

    def test_an_unconfirmed_row_carrying_a_delta_is_not_a_correlation(self, fresh_re_db, monkeypatch):
        """`correlation_confirmed` is the fact; the delta is a detail beside
        it. They are written together today, so dropping the confirmed check
        would change nothing -- until something writes one without the other,
        and then a rejected match would count as a lead."""
        now = _fixed_today(monkeypatch)
        fresh_re_db.create_signal({
            "direction": "BUY", "entry_low": 4000.0, "entry_high": 4002.0,
            "stop_loss": 3995.0, "created_at": now,
            "correlation_time_delta_s": -50.0, "correlation_confirmed": 0,
        })

        led, mean = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 0
        assert mean is None

    def test_a_confirmed_correlation_with_no_delta_is_not_a_lead(self, fresh_re_db, monkeypatch):
        now = _fixed_today(monkeypatch)
        _correlated(fresh_re_db, created_at=now, delta=None)

        led, mean = fresh_re_db.today_lead_stats(now_ts=now)

        assert led == 0
        assert mean is None


def _fixed_today(monkeypatch) -> float:
    """Midnight UTC of a fixed day.

    Passed into every call as `now_ts` so these tests read the same on any
    date. A test whose meaning depends on the calendar is a test that will fail
    one morning for a reason nobody can reproduce.
    """
    import datetime as dt
    return dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc).timestamp()
