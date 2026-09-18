"""The News tab's calendar and blackout window.

The behaviour worth guarding is the one that already went wrong: on 2026-09-04
the page wrote four blackout keys that the calendar's `load()` named none of,
so the settings round-tripped as absent and switching the blackout OFF did
nothing — it was on by default the whole time. So the write here is asserted by
what the CALENDAR reports afterwards, not by what was sent.

Nothing here reaches a broker or the network: the calendar functions are
replaced with stand-ins.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import news as news_router


def _event(**over) -> dict:
    row = {"title": "US Non-Farm Payrolls", "currency": "USD", "impact": "high",
           "ts": 1_750_000_000.0, "forecast": "180K", "previous": "175K",
           "score": 9.5}
    row.update(over)
    return row


@pytest.fixture
def calendar(monkeypatch):
    """The calendar as the controller exposes it, with every read scripted."""
    state = {
        "events": [_event()],
        "current": None,
        "blackout": {"enabled": True, "impact": "high",
                     "minutes_before": 15, "minutes_after": 15},
        "pause": {"paused": False, "label": "", "detail": "",
                  "resume_ts": None, "mins_remaining": None},
        "invalidated": 0,
        "saved": [],
    }
    monkeypatch.setattr(news_router.news_ctl, "get_events", lambda: state["events"])
    monkeypatch.setattr(news_router.news_ctl, "get_current_event", lambda: state["current"])
    monkeypatch.setattr(news_router.news_ctl, "get_blackout_settings", lambda: state["blackout"])
    monkeypatch.setattr(news_router.news_ctl, "news_pause_state", lambda: state["pause"])

    def _invalidate():
        state["invalidated"] += 1

    monkeypatch.setattr(news_router.news_ctl, "invalidate_cache", _invalidate)
    monkeypatch.setattr(news_router.settings_ctl, "save_config",
                        lambda values: state["saved"].append(values))
    return state


def test_the_tab_reads_its_four_pieces_in_one_call(make_client, calendar):
    """Banner, settings and list are always on screen together. Three endpoints
    would let the banner say "blackout active" beside a switch that says off."""
    body = make_client().get("/api/news/state").json()

    assert body["events"] == [_event()]
    assert body["current"] is None
    assert body["blackout"]["minutes_before"] == 15
    assert body["pause"]["paused"] is False


def test_an_event_keeps_the_calendars_own_numbers(make_client, calendar):
    calendar["events"] = [_event(score=3.25, impact="medium", currency="EUR")]

    row = make_client().get("/api/news/state").json()["events"][0]

    assert row["score"] == 3.25
    assert row["impact"] == "medium"
    assert row["currency"] == "EUR"
    assert row["ts"] == 1_750_000_000.0


def test_a_live_blackout_reaches_the_banner_with_its_countdown(make_client, calendar):
    calendar["current"] = _event(mins_remaining=8.0, mins_to_event=-2.0)

    current = make_client().get("/api/news/state").json()["current"]

    assert current["title"] == "US Non-Farm Payrolls"
    assert current["mins_remaining"] == 8.0


def test_saving_the_blackout_writes_the_keys_the_calendar_reads(make_client, calendar):
    """The 2026-09-04 bug, pinned by name. A key the reader does not look at is
    a setting that silently does nothing."""
    make_client().put("/api/news/blackout", json={
        "enabled": False, "impact": "high_medium",
        "minutes_before": 30, "minutes_after": 45,
    })

    assert calendar["saved"] == [{
        "news_blackout_enabled": False,
        "news_blackout_impact": "high_medium",
        "news_blackout_minutes_before": 30,
        "news_blackout_minutes_after": 45,
    }]
    assert set(calendar["saved"][0]) == set(news_router.BLACKOUT_KEYS)


def test_the_impact_level_is_written_too(make_client, calendar):
    """It was in the response schema and in the browser's types from the start
    and was never written, so the picker could not have worked -- the same
    shape as the bug above, one key along."""
    make_client().put("/api/news/blackout", json={
        "enabled": True, "impact": "high_medium",
        "minutes_before": 15, "minutes_after": 15,
    })

    assert calendar["saved"][0]["news_blackout_impact"] == "high_medium"


def test_an_omitted_impact_defaults_to_high_rather_than_blank(make_client, calendar):
    """A blank would be clamped back by the calendar anyway, but writing one
    means the stored value is briefly a string the reader rejects."""
    make_client().put("/api/news/blackout", json={
        "enabled": True, "minutes_before": 15, "minutes_after": 15,
    })

    assert calendar["saved"][0]["news_blackout_impact"] == "high"


def test_the_response_is_what_the_calendar_reports_not_what_was_sent(
    make_client, calendar,
):
    """`get_blackout_settings` clamps the minutes and falls back on an unknown
    impact. Echoing the request would show the operator a window the engine is
    not actually using."""
    calendar["blackout"] = {"enabled": False, "impact": "high",
                            "minutes_before": 120, "minutes_after": 120}

    body = make_client().put("/api/news/blackout", json={
        "enabled": False, "minutes_before": 9999, "minutes_after": 9999,
    }).json()

    assert body["minutes_before"] == 120
    assert body["minutes_after"] == 120


def test_refreshing_drops_the_cache_before_reading(make_client, calendar):
    """A Refresh button that returned the cache would look broken and be
    indistinguishable from a calendar that had not changed."""
    body = make_client().post("/api/news/refresh").json()

    assert calendar["invalidated"] == 1
    assert body["events"] == [_event()]


def test_reading_the_state_does_not_drop_the_cache(make_client, calendar):
    """Negative control. The 5s poll must not re-fetch the upstream feed every
    tick — that is a rate limit waiting to happen."""
    make_client().get("/api/news/state")

    assert calendar["invalidated"] == 0


def test_the_blackout_endpoint_is_not_reachable_by_GET(make_client, calendar):
    assert make_client().get("/api/news/blackout").status_code == 405
