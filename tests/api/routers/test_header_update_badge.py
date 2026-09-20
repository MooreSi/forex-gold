"""The header's "UPDATE AVAILABLE" badge.

The NiceGUI header flashed a badge the moment a `git fetch` found commits on
origin that this checkout did not have, and clicking it opened a popup that
said in plain English what the update changed. The React port shipped neither:
the only place an update appeared was Settings > Node & Updates, which the
operator has to think to open. Asked for on 2026-09-20.

The badge rides the header poll -- one shared poll, per the frontend
conventions -- so what it costs matters. `cached_update_check` answers from
the last check and refreshes behind the caller; this endpoint must use that
and never the live one, or every five-second poll runs a git fetch.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import system as system_router


@pytest.fixture
def header(monkeypatch, sentinel_engine):
    state = {"check": {"available": False}, "live_calls": 0}

    async def _live():
        state["live_calls"] += 1
        return {"available": True}

    monkeypatch.setattr(system_router.trading_ctl, "trading_pause_status",
                        lambda: {"paused": False, "reason": "", "until": None,
                                 "source": ""})
    monkeypatch.setattr(system_router.settings_ctl, "get_active_trader",
                        lambda: "local")
    monkeypatch.setattr(system_router.sync_ctl, "is_connected", lambda: False)
    monkeypatch.setattr(system_router.broker_ctl, "get_effective_ea_status",
                        lambda: (True, "global"))
    monkeypatch.setattr(system_router.broker_ctl, "ea_build_status",
                        lambda: (False, ""))
    monkeypatch.setattr(system_router.broker_ctl, "ea_badge_state",
                        lambda *a: ("green", "EA", "healthy"))
    monkeypatch.setattr(system_router.system_ctl, "cached_update_check",
                        lambda: dict(state["check"]))
    monkeypatch.setattr(system_router.system_ctl, "check_for_update", _live)
    return state


def test_an_install_that_is_up_to_date_shows_no_badge(make_client, header):
    body = make_client().get("/api/system/header").json()

    assert body["update"]["available"] is False


def test_a_pending_update_is_reported_to_the_header(make_client, header):
    header["check"] = {
        "available": True, "local_sha": "aaaaaaa1", "remote_sha": "bbbbbbb2",
        "commits": [{"sha": "b", "short_sha": "bbbbbbb", "summary": "Fix it"}],
    }

    body = make_client().get("/api/system/header").json()

    assert body["update"]["available"] is True


def test_the_badge_says_how_many_commits_are_waiting(make_client, header):
    """"3 new commits" is a different prompt from "an update exists"."""
    header["check"] = {
        "available": True, "local_sha": "a", "remote_sha": "b",
        "commits": [{"summary": "one"}, {"summary": "two"}, {"summary": "three"}],
    }

    body = make_client().get("/api/system/header").json()

    assert body["update"]["commits"] == 3


def test_the_five_second_poll_never_runs_a_live_git_fetch(make_client, header):
    """The whole reason the cache exists."""
    client = make_client()
    for _ in range(3):
        client.get("/api/system/header")

    assert header["live_calls"] == 0


def test_a_check_that_could_not_run_shows_no_badge(make_client, header):
    """A failed fetch is not an update. Flashing a badge the operator cannot
    act on is worse than staying quiet."""
    header["check"] = {"available": False, "error": "git fetch failed"}

    body = make_client().get("/api/system/header").json()

    assert body["update"]["available"] is False
