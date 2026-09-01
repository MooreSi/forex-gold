""""Today's P&L" starts at the user's midnight, not London's.

compute_mt5_performance computes a daily figure by taking every position
closed since midnight. That midnight was hardcoded Europe/London, so a user
in Sydney saw a "today" that began at 09:00 or 10:00 their time and swept in
most of the previous afternoon.

The cutoff is expressed in broker-timestamp space, because that is what the
deals carry: broker_ts = real_utc + 10800.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.broker.mt5_performance import broker_day_cutoff

BROKER_OFFSET = 10800


def _at(*, y=2026, mo=7, d=21, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestTheCutoff:
    def test_it_is_local_midnight_expressed_in_broker_time(self):
        """UTC+0, so local midnight is UTC midnight, and the broker's clock
        reads that instant as 03:00."""
        now = _at(h=14, mi=30)
        cutoff = broker_day_cutoff(offset_minutes=0, now=now)

        assert cutoff == _at(h=0).timestamp() + BROKER_OFFSET

    def test_it_follows_the_clock_east(self):
        """At UTC+10 it is already 00:30 tomorrow, so "today" began half an
        hour ago -- at 14:30 UTC, not at midnight UTC."""
        now = _at(h=14, mi=30)
        cutoff = broker_day_cutoff(offset_minutes=600, now=now)

        assert cutoff == _at(h=14).timestamp() + BROKER_OFFSET

    def test_it_follows_the_clock_west(self):
        """At UTC-8 it is still 06:30 the same morning, and that day began at
        08:00 UTC."""
        now = _at(h=14, mi=30)
        cutoff = broker_day_cutoff(offset_minutes=-480, now=now)

        assert cutoff == _at(h=8).timestamp() + BROKER_OFFSET

    def test_the_cutoff_actually_moves(self):
        """Negative control. A cutoff computed from UTC regardless of the
        offset would satisfy any one of the three above on its own."""
        now = _at(h=14, mi=30)
        cutoffs = {broker_day_cutoff(offset_minutes=off, now=now)
                   for off in (-480, 0, 600)}

        assert len(cutoffs) == 3

    def test_a_uk_summer_clock_gives_what_it_always_gave(self):
        """Characterization. +60 is British Summer Time; London midnight is
        23:00 the previous day in UTC, which is what the hardcoded
        Europe/London produced."""
        now = _at(h=14, mi=30)
        cutoff = broker_day_cutoff(offset_minutes=60, now=now)

        assert cutoff == _at(d=20, h=23).timestamp() + BROKER_OFFSET

    def test_a_moment_just_after_local_midnight_starts_a_new_day(self):
        """The boundary itself, not a point comfortably inside a day."""
        just_after = _at(h=0, mi=1)
        just_before = _at(d=20, h=23, mi=59)

        assert (broker_day_cutoff(offset_minutes=0, now=just_after)
                != broker_day_cutoff(offset_minutes=0, now=just_before))

    def test_the_cutoff_is_never_in_the_future(self):
        for off in (-720, -480, 0, 60, 600, 840):
            now = _at(h=14, mi=30)
            assert broker_day_cutoff(offset_minutes=off, now=now) <= (
                now.timestamp() + BROKER_OFFSET
            )


class TestWithNothingPassed:
    def test_it_asks_the_trading_clock(self, monkeypatch):
        from backend.src.services.risk import clock as risk_clock
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 600)

        now = _at(h=14, mi=30)
        assert broker_day_cutoff(now=now) == broker_day_cutoff(
            offset_minutes=600, now=now
        )

    def test_a_zero_offset_is_honoured(self, monkeypatch):
        """0 is falsy, for the fourth time in this codebase."""
        from backend.src.services.risk import clock as risk_clock
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 0)

        now = _at(h=14, mi=30)
        assert broker_day_cutoff(now=now) == broker_day_cutoff(
            offset_minutes=0, now=now
        )

    def test_it_returns_a_number_with_no_arguments_at_all(self):
        """The real call site passes nothing."""
        assert isinstance(broker_day_cutoff(), float)


class TestItIsTheOneTheStatsUse:
    def test_compute_mt5_performance_calls_it(self):
        """Otherwise the function above is correct and unused, and the daily
        figure still starts at a London midnight."""
        import ast
        import pathlib

        from backend.src.services.broker import mt5_performance as perf

        src = pathlib.Path(perf.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "compute_mt5_performance")

        assert any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                   and c.func.id == "broker_day_cutoff"
                   for c in ast.walk(fn))

    def test_the_stats_no_longer_hardcode_london(self):
        """Code, not prose. The docstring on broker_day_cutoff names
        Europe/London to say what it replaced, and a bare substring test reads
        that as the violation it is describing."""
        import pathlib

        from backend.src.services.broker import mt5_performance as perf

        src = pathlib.Path(perf.__file__).read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines()
                if "Europe/London" in ln and not ln.strip().startswith("#")
                and '"""' not in ln]
        code = [ln for ln in code if "ZoneInfo" in ln]

        assert code == [], code
        assert "ZoneInfo" not in src, "the timezone import is still here"
