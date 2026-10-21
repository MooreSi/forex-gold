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


# ── The chart's NEWS button ──────────────────────────────────────────────────
# The kept history holds high and medium only (news_history.KEEP_IMPACTS), so
# the chart's low-impact events come from the live feed's current week. Both
# sources describe the same releases, and one release must not be drawn twice.

def _feed(monkeypatch, rows):
    monkeypatch.setattr(nc, "_cache_events", rows, raising=False)


def _raw(when, title, impact, currency="USD"):
    return {"date": when.isoformat(), "title": title, "country": currency,
            "impact": impact, "forecast": "0.3%", "previous": "0.2%"}


def test_the_chart_gets_kept_history_and_the_live_weeks_low_impact_events(monkeypatch):
    _keep([ev(_ts(2026, 9, 15, 12, 30), "Old CPI")])
    _feed(monkeypatch, [
        _raw(datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc), "Pending Home Sales", "Low"),
        _raw(datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc), "Core PCE", "High"),
    ])

    got = cn.for_chart(_ts(2026, 9, 1), _ts(2026, 9, 30))

    assert [(e["title"], e["impact"]) for e in got] == [
        ("Old CPI", "high"), ("Pending Home Sales", "low"), ("Core PCE", "high")]
    assert got[1]["forecast"] == "0.3%"


def test_a_release_in_both_the_history_and_the_feed_comes_back_once(monkeypatch):
    when = datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc)
    _keep([ev(when.timestamp(), "Core PCE")])
    _feed(monkeypatch, [_raw(when, "Core PCE", "High")])

    assert [e["title"] for e in cn.for_chart(_ts(2026, 9, 1), _ts(2026, 9, 30))] == ["Core PCE"]


def test_holidays_and_events_outside_the_window_are_left_out(monkeypatch):
    _feed(monkeypatch, [
        _raw(datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc), "Bank Holiday", "Holiday"),
        _raw(datetime(2026, 10, 5, 12, 30, tzinfo=timezone.utc), "Too late", "Low"),
        _raw(datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc), "Kept", "Medium"),
    ])

    assert [e["title"] for e in cn.for_chart(_ts(2026, 9, 1), _ts(2026, 9, 30))] == ["Kept"]
