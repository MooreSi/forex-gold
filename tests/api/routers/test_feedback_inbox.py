"""The admin node's new-feedback alert: header count, inbox read, mark read.

The count rides the header poll; the entries themselves are fetched only when
the popup opens, because a message can be 4000 characters and the header is
polled every five seconds. The controller is replaced with recorders.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import feedback as feedback_router
from backend.src.api.routers import system as system_router

ROW = {"id": "a" * 32, "kind": "bug", "message": "It broke", "name": "Sam",
       "email": "s@x.co", "hostname": "mac-01", "version": "1.2.3",
       "submitted_at": 1000.0}


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
    monkeypatch.setattr(system_router.feedback_ctl, "completed_notices", lambda: [])
    state = {"unread": 0}
    monkeypatch.setattr(system_router.feedback_ctl, "unread_count",
                        lambda: state["unread"])
    return state


def test_the_header_carries_the_unread_count(make_client, header):
    header["unread"] = 3

    body = make_client().get("/api/system/header").json()

    assert body["feedback_unread"] == 3


@pytest.fixture
def inbox(monkeypatch):
    state = {"rows": [ROW], "marked": []}
    monkeypatch.setattr(feedback_router.feedback_ctl, "unread_feedback",
                        lambda: list(state["rows"]))

    def _mark(ids):
        state["marked"].append(list(ids))
        return len(ids)
    monkeypatch.setattr(feedback_router.feedback_ctl, "mark_read", _mark)
    return state


def test_the_inbox_lists_unread_feedback(make_client, inbox):
    resp = make_client().get("/api/feedback/inbox")

    assert resp.status_code == 200
    assert resp.json() == [ROW]


def test_marking_read_forwards_the_ids_shown(make_client, inbox):
    resp = make_client().post("/api/feedback/inbox/read", json={"ids": ["a" * 32]})

    assert resp.status_code == 200 and resp.json() == {"ok": True, "marked": 1}
    assert inbox["marked"] == [["a" * 32]]
