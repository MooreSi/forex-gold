"""The "Feedback noted and completed" popup's two endpoints.

Completed notices ride the header poll -- one shared poll, per the frontend
conventions -- and Close posts back that the notice was seen. The controller
is replaced with recorders, so nothing is read or written on disk.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import feedback as feedback_router
from backend.src.api.routers import system as system_router

NOTICE = {"id": "a" * 32, "kind": "feature", "message": "Dark mode please",
          "completed_at": 1700000000.0}


@pytest.fixture
def header(monkeypatch, sentinel_engine):
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
                        lambda: {"available": False})
    state = {"notices": []}
    monkeypatch.setattr(system_router.feedback_ctl, "completed_notices",
                        lambda: list(state["notices"]))
    return state


def test_the_header_carries_completed_notices(make_client, header):
    header["notices"] = [NOTICE]

    body = make_client().get("/api/system/header").json()

    assert body["feedback_done"] == [NOTICE]


def test_no_notices_is_an_empty_list(make_client, header):
    body = make_client().get("/api/system/header").json()

    assert body["feedback_done"] == []


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def _mark(entry_id):
        calls.append(entry_id)
        return entry_id == "a" * 32
    monkeypatch.setattr(feedback_router.feedback_ctl, "mark_seen", _mark)
    return calls


def test_closing_the_popup_marks_it_seen(make_client, seen):
    resp = make_client().post(f"/api/feedback/notices/{'a' * 32}/seen")

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert seen == ["a" * 32]


def test_an_unknown_notice_is_a_404(make_client, seen):
    resp = make_client().post("/api/feedback/notices/nope/seen")

    assert resp.status_code == 404
