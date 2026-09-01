"""Which day a trade belongs to is the user's day, not London's.

The monthly P&L calendar, the heatmap and the "today" figure all grouped
closed trades by a hardcoded Europe/London calendar date. For the owner, who
is in the UK, that is correct by coincidence. For anyone else it files trades
under the wrong day -- a trade closed at 08:00 in Sydney lands on the previous
day's square, and the day's P&L is a number from two different days.

The trading clock (services/risk/clock) now exists for exactly this, so the
day boundary follows it.

What deliberately does NOT change: anything anchored to the London *market
session*. The ORB report, its 08:15 email and the London-session breakout
gates are about when London opens, not about where the user is, and a trader
in Tokyo still wants them on London time.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.src.controllers import history_controller as history

# 2026-07-22 01:00 as the broker stores it (UTC+3 written as if it were UTC).
# Real UTC is 2026-07-21 22:00.
BROKER_TS = 1784682000


class TestTheDayFollowsTheClock:
    def test_the_offset_is_removed_before_the_date_is_taken(self):
        """The reason this function exists. A date read straight off the
        broker timestamp files a trade closed late in the evening under the
        following day."""
        assert history.broker_ts_to_local_date(BROKER_TS, offset_minutes=0) == date(2026, 7, 21)

    def test_a_uk_summer_clock_gives_the_same_day_as_before(self):
        """Characterization. +60 is British Summer Time, which is what the old
        hardcoded Europe/London gave for this timestamp: 23:00 on the 21st."""
        assert history.broker_ts_to_local_date(BROKER_TS, offset_minutes=60) == date(2026, 7, 21)

    def test_a_clock_far_enough_east_moves_it_to_the_next_day(self):
        """The bug, stated as a test. 22:00 UTC is already 08:00 the next
        morning in Sydney, and that trade belongs on the next day's square."""
        assert history.broker_ts_to_local_date(BROKER_TS, offset_minutes=600) == date(2026, 7, 22)

    def test_a_clock_far_enough_west_moves_it_to_the_previous_day(self):
        """A different timestamp, real UTC 2026-07-21 02:00, because 22:00 UTC
        is still the same day everywhere west of it. In Los Angeles this one
        is 18:00 the previous evening."""
        assert history.broker_ts_to_local_date(1784610000, offset_minutes=-480) == date(2026, 7, 20)

    def test_the_day_actually_moves_with_the_clock(self):
        """Negative control for the three above. A function that ignored the
        offset entirely would pass any single one of them."""
        days = {history.broker_ts_to_local_date(BROKER_TS, offset_minutes=off)
                for off in (-480, 0, 600)}

        assert len(days) > 1, "the offset changed nothing"


class TestWithNoOffsetGiven:
    def test_it_asks_the_trading_clock(self, monkeypatch):
        """Callers pass no offset; the function resolves it. Pinned by
        substitution rather than by the machine's own zone, because a test
        that asserts the machine's answer passes everywhere and proves
        nothing anywhere."""
        from backend.src.services.risk import clock as risk_clock
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 600)

        assert history.broker_ts_to_local_date(BROKER_TS) == date(2026, 7, 22)

    def test_none_means_this_machine_and_is_not_confused_with_unset(self,
                                                                    monkeypatch):
        """`offset_minutes()` returns None for "use this machine's own clock",
        which is a real answer, not a missing one. A default of None instead
        of a sentinel would make an explicit "machine clock" indistinguishable
        from "you decide" -- harmless here, and the kind of thing that stops
        being harmless the moment a caller passes it through."""
        from backend.src.services.risk import clock as risk_clock
        from backend.src.utils.trading_clock import local_from_timestamp
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: None)

        expected = local_from_timestamp(BROKER_TS - 10800, None).date()

        assert history.broker_ts_to_local_date(BROKER_TS) == expected

    def test_a_zero_offset_is_honoured_and_not_read_as_unset(self, monkeypatch):
        """UTC+0 again. 0 is falsy, and this is the third place in this
        codebase where treating it as absent would silently do something
        else."""
        from backend.src.services.risk import clock as risk_clock
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 0)

        assert history.broker_ts_to_local_date(BROKER_TS) == date(2026, 7, 21)


class TestJunk:
    @pytest.mark.parametrize("bad", [None, "", "not-a-timestamp", object()])
    def test_it_returns_none_rather_than_raising(self, bad):
        """This runs per row while building a calendar. One unparseable
        timestamp must not take the page down."""
        assert history.broker_ts_to_local_date(bad) is None


class TestTodayOnTheTradingClock:
    def test_the_controller_offers_it(self):
        from backend.src.controllers import system_controller as sysctl

        assert isinstance(sysctl.local_today(), date)

    def test_it_follows_the_trading_clock(self, monkeypatch):
        from backend.src.controllers import system_controller as sysctl
        from backend.src.services.risk import clock as risk_clock

        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: -720)
        west = sysctl.local_today()
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 840)
        east = sysctl.local_today()

        assert east >= west
        assert (east - west).days <= 2


class TestLondonStaysLondon:
    """The session-anchored surfaces must not have been swept up in this."""

    @pytest.mark.parametrize("rel", [
        "backend/src/services/analytics/orb_report.py",
        "backend/src/services/notifications/scheduler.py",
    ])
    def test_the_london_session_surfaces_still_use_london(self, rel):
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        src = (repo / rel).read_text(encoding="utf-8")

        assert "Europe/London" in src, (
            f"{rel} is anchored to the London market session, not to where the "
            f"user is. Moving it onto the trading clock would send a trader in "
            f"Tokyo the London Open report at Tokyo's open."
        )
