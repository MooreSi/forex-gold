"""POST /api/remote/restart-vps: the Mac asks its VPS to restart (owner, 2026-09-26).

Nothing here reaches a socket: the sync controller is a recorder. What is
tested is that a request only goes out on a live link, and that every way it
can fail reaches the operator as a reason rather than as a claimed restart.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.routers import remote as remote_router


@pytest.fixture
def link(monkeypatch):
    state = {"connected": True, "reply": {"ok": True, "note": "Restarting"},
             "raises": None, "calls": 0}

    async def _restart_peer():
        state["calls"] += 1
        if state["raises"]:
            raise state["raises"]
        return state["reply"]

    monkeypatch.setattr(remote_router.sync_ctl, "is_connected", lambda: state["connected"])
    monkeypatch.setattr(remote_router.sync_ctl, "restart_peer", _restart_peer)
    return state


def _post(make_client):
    return make_client().post("/api/remote/restart-vps", json={})


def test_it_asks_the_vps_and_passes_on_its_answer(make_client, link):
    res = _post(make_client)

    assert res.status_code == 200
    assert link["calls"] == 1
    assert "Restarting" in res.json()["note"]


def test_not_connected_sends_nothing(make_client, link):
    link["connected"] = False

    res = _post(make_client)

    assert res.status_code == 409
    assert link["calls"] == 0


def test_a_vps_that_could_not_restart_is_a_refusal(make_client, link):
    link["reply"] = {"ok": False, "note": "Restart failed: no run.py"}

    res = _post(make_client)

    assert res.status_code == 409
    assert "Restart failed: no run.py" in res.json()["error"]["message"]


def test_no_answer_says_the_vps_may_need_updating(make_client, link):
    link["raises"] = asyncio.TimeoutError()

    res = _post(make_client)

    assert res.status_code == 409
    assert "update" in res.json()["error"]["message"].lower()


def test_a_dropped_link_is_a_refusal(make_client, link):
    link["raises"] = ConnectionError("not connected to VPS")

    res = _post(make_client)

    assert res.status_code == 409
    assert link["calls"] == 1
