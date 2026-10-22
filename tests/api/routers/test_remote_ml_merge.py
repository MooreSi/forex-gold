"""Settings > Remote > ML model snapshot > Merge training data (docs/todo/026).

The route starts a merge in the background and reports on it; the merge
itself is pinned in tests/ml_merge. **Nothing here reaches a socket**: the
controller is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import remote as remote_router


@pytest.fixture
def merge(monkeypatch):
    state = {"connected": True, "started": 0, "raises": None}

    async def _start():
        if state["raises"]:
            raise state["raises"]
        state["started"] += 1

    async def _status():
        return {"running": False, "stage": "", "error": None, "result": None,
                "datasets": {"re": {"imported": 12, "decision": None}}}

    monkeypatch.setattr(remote_router.sync_ctl, "is_connected", lambda: state["connected"])
    monkeypatch.setattr(remote_router.merge_ctl, "start_merge", _start)
    monkeypatch.setattr(remote_router.merge_ctl, "merge_status", _status)
    return state


def test_it_starts_a_merge_and_says_so(make_client, merge):
    res = make_client().post("/api/remote/ml-merge")

    assert res.status_code == 200
    assert merge["started"] == 1
    assert "started" in res.json()["note"].lower()


def test_without_the_vps_link_nothing_starts(make_client, merge):
    merge["connected"] = False

    res = make_client().post("/api/remote/ml-merge")

    assert res.status_code >= 400
    assert merge["started"] == 0
    assert "vps" in res.json()["error"]["message"].lower()


def test_a_merge_already_running_is_refused_with_its_reason(make_client, merge):
    merge["raises"] = remote_router.merge_ctl.MergeFailed("A merge is already running.")

    res = make_client().post("/api/remote/ml-merge")

    assert res.status_code >= 400
    assert "already running" in res.json()["error"]["message"]


def test_the_status_is_read_back(make_client, merge):
    body = make_client().get("/api/remote/ml-merge").json()

    assert body["datasets"]["re"]["imported"] == 12
    assert body["running"] is False
