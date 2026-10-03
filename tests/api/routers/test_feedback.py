"""The Feedback popup's endpoint.

It only accepts text. A refusal carries the reason the user can act on, and a
success returns the id; delivery is the service's job and is pinned in
tests/feedback. The controller is replaced with a recorder, so nothing is
queued, sent or written.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import feedback as feedback_router


@pytest.fixture
def ctl(monkeypatch):
    state = {"calls": [], "refuse": None}

    def _submit(kind, message):
        if state["refuse"]:
            raise ValueError(state["refuse"])
        state["calls"].append((kind, message))
        return {"id": "abc123"}
    monkeypatch.setattr(feedback_router.feedback_ctl, "submit", _submit)
    return state


def test_a_submission_is_forwarded_and_acknowledged(make_client, ctl):
    resp = make_client().post("/api/feedback", json={"kind": "bug", "message": "It broke"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": "abc123"}
    assert ctl["calls"] == [("bug", "It broke")]


def test_a_refusal_reaches_the_user(make_client, ctl):
    ctl["refuse"] = "Write something first."

    resp = make_client().post("/api/feedback", json={"kind": "bug", "message": " "})

    assert resp.status_code == 400
    assert "Write something first." in resp.text
