"""The brain view's endpoint: one read-only GET (docs/todo/008).

Nothing here reaches a broker: the controller is replaced with a fake.
"""
from __future__ import annotations

from backend.src.api.routers import brain as brain_router


def test_the_brain_reads_in_one_call(make_client, monkeypatch):
    async def _snap():
        return {"now": 1.0, "gates": [{"key": "halt", "blocking": False}],
                "events": [{"key": "tg:1", "gate": "broker"}]}

    monkeypatch.setattr(brain_router.brain_ctl, "snapshot_async", _snap)
    body = make_client().get("/api/brain").json()
    assert body["gates"][0]["key"] == "halt"
    assert body["events"][0]["key"] == "tg:1"


def test_it_has_no_write_routes():
    methods = {m for r in brain_router.router.routes for m in r.methods}
    assert methods == {"GET"}
