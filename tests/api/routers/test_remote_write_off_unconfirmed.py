"""POST /api/remote/write-off-unconfirmed: the Mac asks the VPS to write off
placeholders its broker has no record of (owner, 2026-09-28).

Nothing here reaches a socket: the sync controller is a recorder. What is
tested is that a request only goes out on a live link, that the note says
how many were written off and why any were kept, and that every failure is a
refusal rather than a claimed write-off.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.routers import remote as remote_router


@pytest.fixture
def link(monkeypatch):
    state = {"connected": True, "calls": 0, "raises": None,
             "reply": {"written_off": ["1f5801a6-4450-41", "3041d252-f618-4a"],
                       "kept": [], "error": None}}

    async def _write_off():
        state["calls"] += 1
        if state["raises"]:
            raise state["raises"]
        return state["reply"]

    monkeypatch.setattr(remote_router.sync_ctl, "is_connected", lambda: state["connected"])
    monkeypatch.setattr(remote_router.sync_ctl, "write_off_peer_unconfirmed", _write_off)
    return state


def _post(make_client):
    return make_client().post("/api/remote/write-off-unconfirmed", json={})


def test_it_asks_the_vps_and_says_how_many(make_client, link):
    res = _post(make_client)

    assert res.status_code == 200
    assert link["calls"] == 1
    assert "2" in res.json()["note"]


def test_a_kept_row_is_named_with_its_reason(make_client, link):
    link["reply"] = {"written_off": [], "error": None,
                     "kept": [{"trade_id": "1f5801a6-4450-41",
                               "reason": "the broker has a deal for it"}]}

    note = _post(make_client).json()["note"]

    assert "1f5801a6" in note
    assert "the broker has a deal for it" in note


def test_not_connected_sends_nothing(make_client, link):
    link["connected"] = False

    res = _post(make_client)

    assert res.status_code == 409
    assert link["calls"] == 0


def test_an_unreadable_broker_is_a_refusal(make_client, link):
    link["reply"] = {"written_off": [], "kept": [],
                     "error": "The broker could not be read. Nothing was written off."}

    res = _post(make_client)

    assert res.status_code == 409
    assert "Nothing was written off" in res.json()["error"]["message"]


def test_no_answer_says_the_vps_may_need_updating(make_client, link):
    link["raises"] = asyncio.TimeoutError()

    res = _post(make_client)

    assert res.status_code == 409
    assert "update" in res.json()["error"]["message"].lower()
