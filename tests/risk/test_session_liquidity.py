"""Scheduled illiquidity: the windows where the spread is the trade.

Section 5.6 of docs/todo/reversal-engine/200. `utils/news_calendar.py`
already tiers events by impact and blacks out around them. What nothing
covers is the illiquidity that arrives on a CLOCK rather than a calendar:
the daily rollover, the Sunday reopen, and the last hours of a month or
quarter. Those are not news; they are hours in which the spread widens, the
book thins and a stop is cheap to reach.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.risk import session_liquidity as sl


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


class TestRollover:
    def test_the_rollover_window_is_flagged(self):
        """The daily swap/rollover window. Spreads on gold routinely go to
        several times normal here and the book is at its thinnest."""
        ok, reason = sl.check(at(2026, 9, 9, 22, 0), sl.Config())
        assert ok is False
        assert "rollover" in reason.lower()

    def test_an_hour_either_side_is_fine(self):
        assert sl.check(at(2026, 9, 9, 20, 30), sl.Config())[0] is True
        assert sl.check(at(2026, 9, 9, 23, 30), sl.Config())[0] is True

    def test_the_window_is_configurable_and_can_be_switched_off(self):
        cfg = sl.Config(rollover_enabled=False)
        assert sl.check(at(2026, 9, 9, 22, 0), cfg)[0] is True


class TestSundayOpen:
    def test_the_first_minutes_of_the_week_are_flagged(self):
        """2026-09-06 is a Sunday. The reopen carries the weekend's gap and
        the widest spreads of the week."""
        ok, reason = sl.check(at(2026, 9, 6, 22, 10), sl.Config())
        assert ok is False
        assert "reopen" in reason.lower()

    def test_once_the_open_has_settled_trading_resumes(self):
        assert sl.check(at(2026, 9, 7, 2, 0), sl.Config())[0] is True


class TestPeriodEnd:
    def test_the_last_hours_of_a_month_are_flagged_when_enabled(self):
        """Month-end rebalancing flow is real and it is not price
        discovery. Off by default because it costs trading days."""
        ts = at(2026, 9, 30, 15, 30)
        assert sl.check(ts, sl.Config())[0] is True
        assert sl.check(ts, sl.Config(period_end_enabled=True))[0] is False

    def test_a_normal_day_is_not_period_end(self):
        assert sl.check(at(2026, 9, 15, 15, 30),
                        sl.Config(period_end_enabled=True))[0] is True


class TestDefaultsChangeNothing:
    def test_an_ordinary_london_morning_is_allowed(self):
        assert sl.check(at(2026, 9, 9, 9, 0), sl.Config()) == (True, "")

    def test_every_window_off_allows_everything(self):
        cfg = sl.Config(rollover_enabled=False, weekend_reopen_enabled=False,
                        period_end_enabled=False)
        assert sl.check(at(2026, 9, 6, 22, 5), cfg)[0] is True
        assert sl.check(at(2026, 9, 9, 22, 0), cfg)[0] is True
