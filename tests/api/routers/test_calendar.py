"""The Calendar API (docs/todo/023): the day journal and the news history.
Forwarding and shape checks only; storage is tested in tests/analytics. No
route here reaches a broker: the sentinel engine fails any test that does."""
from __future__ import annotations

import pytest

from backend.src.api.routers import calendar as router_mod


@pytest.fixture
def ctl(monkeypatch):
    calls: list[tuple] = []
    ctl = router_mod.calendar_ctl

    async def _journal(start, end):
        calls.append(("journal", start, end))
        return [{"day": "2026-09-15", "note": "n", "tags": ["a"], "mood": 3, "updated_at": 1.0}]

    async def _save(day, note, tags, mood):
        calls.append(("save", day, note, tags, mood))
        if note == "refuse":
            raise ctl.BadJournalEntry("Too many tags.")
        return {"day": day, "note": note, "tags": tags, "mood": mood, "updated_at": 2.0}

    async def _news(start, end):
        calls.append(("news", start, end))
        return [{"day": "2026-09-15", "ts": 1.0, "title": "CPI", "currency": "USD",
                 "impact": "high"}]

    monkeypatch.setattr(ctl, "journal_between", _journal)
    monkeypatch.setattr(ctl, "save_journal", _save)
    monkeypatch.setattr(ctl, "news_between", _news)
    return calls


def test_reads_the_journal_for_a_range(make_client, ctl):
    r = make_client().get("/api/calendar/journal?start=2026-09-01&end=2026-09-31")
    assert r.status_code == 200
    assert r.json()["entries"][0]["note"] == "n"
    assert ctl == [("journal", "2026-09-01", "2026-09-31")]


def test_saves_a_day(make_client, ctl):
    r = make_client().put("/api/calendar/journal/2026-09-15",
                          json={"note": "clean", "tags": ["patient"], "mood": 4})
    assert r.status_code == 200
    assert ctl == [("save", "2026-09-15", "clean", ["patient"], 4)]


def test_a_refused_entry_is_a_422_with_the_reason(make_client, ctl):
    r = make_client().put("/api/calendar/journal/2026-09-15",
                          json={"note": "refuse", "tags": [], "mood": None})
    assert r.status_code == 422
    assert "Too many tags" in r.text


@pytest.mark.parametrize("path,body", [
    ("/api/calendar/journal/15-09-2026", {"note": "", "tags": [], "mood": None}),
    ("/api/calendar/journal/2026-09-15", {"note": "", "tags": [], "mood": 7}),
])
def test_bad_shapes_never_reach_the_controller(make_client, ctl, path, body):
    assert make_client().put(path, json=body).status_code == 422
    assert ctl == []


def test_a_bad_range_never_reaches_the_controller(make_client, ctl):
    assert make_client().get("/api/calendar/journal?start=yesterday&end=2026-09-30").status_code == 422
    assert ctl == []


def test_reads_the_news_for_a_range(make_client, ctl):
    r = make_client().get("/api/calendar/news?start=2026-09-01&end=2026-09-31")
    assert r.status_code == 200
    assert r.json()["events"][0]["title"] == "CPI"
    assert ctl == [("news", "2026-09-01", "2026-09-31")]


@pytest.fixture
def chart_news(monkeypatch):
    calls: list[tuple] = []

    async def _for_chart(from_ts, to_ts):
        calls.append((from_ts, to_ts))
        return [{"ts": 5.0, "title": "NFP", "currency": "USD", "impact": "low",
                 "forecast": "", "previous": ""}]

    monkeypatch.setattr(router_mod.calendar_ctl, "news_for_chart", _for_chart)
    return calls


def test_reads_the_news_for_the_chart_by_instant(make_client, chart_news):
    r = make_client().get("/api/calendar/chart_news?from_ts=100&to_ts=200")
    assert r.status_code == 200
    assert r.json()["events"][0]["impact"] == "low"
    assert chart_news == [(100.0, 200.0)]


@pytest.mark.parametrize("query", [
    "from_ts=200&to_ts=100",           # backwards
    "from_ts=0&to_ts=40000000",        # more than 400 days
    "from_ts=-5&to_ts=100",
])
def test_a_bad_chart_window_never_reaches_the_controller(make_client, chart_news, query):
    assert make_client().get(f"/api/calendar/chart_news?{query}").status_code == 422
    assert chart_news == []
