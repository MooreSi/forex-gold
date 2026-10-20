"""News events for the Calendar, filed by trading day (docs/todo/023, #9).

An event is a real UTC instant; the calendar's days are the trading clock's.
The same clock must file both, or a 23:30 UTC release lands a day away from
the trades it moved.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.src.services.analytics import calendar_news as cn
from backend.src.utils import news_calendar as nc
from backend.src.utils import news_history as nh


def _ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def _keep(events):
    nh.merge(nc.history_file(), events, now=_ts(2026, 9, 20))


def ev(ts, title="CPI"):
    return {"ts": ts, "title": title, "currency": "USD", "impact": "high",
            "forecast": "", "previous": ""}


def test_events_in_the_range_come_back_with_their_trading_day(monkeypatch):
    monkeypatch.setattr(cn._clock, "offset_minutes", lambda: 0)
    _keep([ev(_ts(2026, 9, 15, 12, 30)), ev(_ts(2026, 10, 2, 12, 30), "Later")])

    got = cn.between("2026-09-01", "2026-09-30")
    assert [(e["day"], e["title"]) for e in got] == [("2026-09-15", "CPI")]


def test_the_trading_clock_decides_the_day(monkeypatch):
    # 23:30 UTC on the 15th is the 16th on a clock two hours ahead.
    monkeypatch.setattr(cn._clock, "offset_minutes", lambda: 120)
    _keep([ev(_ts(2026, 9, 15, 23, 30))])

    assert [e["day"] for e in cn.between("2026-09-01", "2026-09-30")] == ["2026-09-16"]


def test_an_event_just_outside_the_range_is_left_out(monkeypatch):
    monkeypatch.setattr(cn._clock, "offset_minutes", lambda: 0)
    _keep([ev(_ts(2026, 8, 31, 23, 59))])

    assert cn.between("2026-09-01", "2026-09-30") == []
