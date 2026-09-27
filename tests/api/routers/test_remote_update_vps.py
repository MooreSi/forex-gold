"""POST /api/remote/update-vps and GET /api/remote/versions (owner, 2026-09-27).

Nothing here reaches a socket or git: the sync controller is a recorder. A
request only goes out on a live link, and every way it can fail reaches the
operator as a reason rather than a claimed update.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.routers import remote as remote_router


@pytest.fixture
def link(monkeypatch):
    state = {"connected": True, "reply": {"ok": True, "note": "Updating"},
             "raises": None, "calls": 0}

    async def _update_peer():
        state["calls"] += 1
        if state["raises"]:
            raise state["raises"]
        return state["reply"]

    monkeypatch.setattr(remote_router.sync_ctl, "is_connected", lambda: state["connected"])
    monkeypatch.setattr(remote_router.vps_ctl, "update_peer", _update_peer)
    return state


def _post(make_client):
    return make_client().post("/api/remote/update-vps", json={})


def test_it_asks_the_vps_and_passes_on_its_answer(make_client, link):
    res = _post(make_client)
    assert res.status_code == 200
    assert link["calls"] == 1
    assert "Updating" in res.json()["note"]


def test_no_link_sends_nothing(make_client, link):
    link["connected"] = False
    res = _post(make_client)
    assert res.status_code >= 400
    assert link["calls"] == 0


def test_a_refusal_is_passed_on(make_client, link):
    link["reply"] = {"ok": False, "note": "An update is already running on the VPS."}
    res = _post(make_client)
    assert res.status_code >= 400
    assert "already running" in res.text


def test_an_older_vps_that_never_answers_is_explained(make_client, link):
    link["raises"] = asyncio.TimeoutError()
    res = _post(make_client)
    assert res.status_code >= 400
    assert "older version" in res.text


def test_versions_reads_in_one_call(make_client, monkeypatch):
    monkeypatch.setattr(remote_router.vps_ctl, "version_report", lambda: {
        "local": {"commit": "a" * 40, "git_version": "2.39.5"},
        "remote": {"commit": "a" * 40, "git_version": "2.45.1"},
        "in_sync": True, "last_update": None})
    body = make_client().get("/api/remote/versions").json()
    assert body["in_sync"] is True
    assert body["remote"]["git_version"] == "2.45.1"
