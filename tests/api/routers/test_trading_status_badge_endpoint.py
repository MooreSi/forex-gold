"""The header badge's endpoints.

Neither places or closes an order. The badge is a read; the resume restores
the normal state by clearing a hold a human or a guard put in place.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import trading_status as router_mod


@pytest.fixture
def lab(monkeypatch):
    state = {
        "badge": {"state": "ok", "label": "Trading Active",
                  "detail": "Nothing is holding automated entries.",
                  "until": None, "resume_ts": None, "can_resume": False},
        "resumed": [],
    }

    def _resume():
        state["resumed"].append(True)
        return {"cleared": ["circuit_breaker"], "status": dict(state["badge"])}

    monkeypatch.setattr(router_mod.status_ctl, "trading_status_badge",
                        lambda: dict(state["badge"]))
    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_all", _resume)
    return state


def test_the_badge_is_readable(make_client, lab):
    body = make_client().get("/api/trading/status-badge").json()

    assert body["state"] == "ok"
    assert body["label"] == "Trading Active"


def test_a_halt_reaches_the_header_with_its_reason(make_client, lab):
    lab["badge"] = {"state": "halted", "label": "Trading Paused until 20 Sep 09:00",
                    "detail": "Circuit breaker active (3 consecutive losses)",
                    "until": 1.0, "resume_ts": None, "can_resume": True}

    body = make_client().get("/api/trading/status-badge").json()

    assert body["state"] == "halted"
    assert "consecutive losses" in body["detail"]
    assert body["can_resume"] is True


def test_reading_the_badge_never_resumes_anything(make_client, lab):
    """A GET that cleared a halt would be one browser prefetch from
    re-enabling trading nobody asked to re-enable."""
    make_client().get("/api/trading/status-badge")

    assert lab["resumed"] == []


def test_resume_clears_and_says_what_it_cleared(make_client, lab):
    body = make_client().post("/api/trading/resume-all").json()

    assert lab["resumed"] == [True]
    assert body["cleared"] == ["circuit_breaker"]


def test_resume_states_that_the_daily_target_is_today_only(make_client, lab):
    """Presenting it as a setting would describe behaviour the backend does
    not have -- it returns at the day boundary on its own."""
    body = make_client().post("/api/trading/resume-all").json()

    assert "today only" in body["note"]


def test_resume_is_not_reachable_by_GET(make_client, lab):
    assert make_client().get("/api/trading/resume-all").status_code == 405
    assert lab["resumed"] == []
