"""The Feedback popup's optional reply email reaches the controller.

Sent only when the sender typed one: an older dashboard posting just kind and
message must keep working unchanged.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import feedback as feedback_router


@pytest.fixture
def ctl(monkeypatch):
    calls = []

    def _submit(kind, message, **kw):
        calls.append((kind, message, kw))
        return {"id": "abc123"}
    monkeypatch.setattr(feedback_router.feedback_ctl, "submit", _submit)
    return calls


def test_a_reply_email_is_forwarded(make_client, ctl):
    resp = make_client().post("/api/feedback", json={
        "kind": "bug", "message": "It broke", "email": "sam@reply.co"})

    assert resp.status_code == 200
    assert ctl == [("bug", "It broke", {"email": "sam@reply.co"})]


def test_without_one_only_kind_and_message_go(make_client, ctl):
    make_client().post("/api/feedback", json={"kind": "bug", "message": "It broke"})

    assert ctl == [("bug", "It broke", {})]
