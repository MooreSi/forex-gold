"""The Breakout tuning experiment endpoints (docs/todo/007).

Reads the card in one call; approve, reject and the approval switch each echo
the new state. A refusal from the ledger (not a waiting proposal, one already
running) reaches the operator in its own words as a 400.

Nothing here reaches a broker: the controller is replaced with fakes.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import tuning as tuning_router


@pytest.fixture
def svc(monkeypatch):
    state = {
        "card": {"approval_required": False, "min_sample": 30, "failure_usd": 100.0,
                 "running": None, "proposals": [{"id": 7, "param": "min_adx_go"}],
                 "history": []},
        "calls": [],
        "refuse": None,
    }

    async def _state():
        return state["card"]

    def _decide(name):
        async def _fn(exp_id):
            if state["refuse"]:
                raise ValueError(state["refuse"])
            state["calls"].append((name, exp_id))
            return {"id": exp_id}
        return _fn

    async def _mode(on):
        state["calls"].append(("mode", on))
        state["card"] = {**state["card"], "approval_required": on}

    ctl = tuning_router.tuning_ctl
    monkeypatch.setattr(ctl, "state_async", _state)
    monkeypatch.setattr(ctl, "approve_async", _decide("approve"))
    monkeypatch.setattr(ctl, "reject_async", _decide("reject"))
    monkeypatch.setattr(ctl, "set_approval_required_async", _mode)
    return state


def test_the_card_reads_in_one_call(make_client, svc):
    body = make_client().get("/api/engines/breakout/tuning").json()
    assert body["proposals"][0]["param"] == "min_adx_go"
    assert body["min_sample"] == 30


def test_approve_forwards_the_id_and_echoes_the_state(make_client, svc):
    resp = make_client().post("/api/engines/breakout/tuning/7/approve")
    assert resp.status_code == 200
    assert svc["calls"] == [("approve", 7)]
    assert "proposals" in resp.json()


def test_reject_forwards_the_id(make_client, svc):
    make_client().post("/api/engines/breakout/tuning/7/reject")
    assert svc["calls"] == [("reject", 7)]


def test_a_refusal_reaches_the_operator_in_its_own_words(make_client, svc):
    svc["refuse"] = "An experiment is already running. One change at a time."
    resp = make_client().post("/api/engines/breakout/tuning/7/approve")
    assert resp.status_code == 400
    assert "already running" in resp.text


def test_the_approval_switch(make_client, svc):
    resp = make_client().put("/api/engines/breakout/tuning/mode",
                             json={"approval_required": True})
    assert svc["calls"] == [("mode", True)]
    assert resp.json()["approval_required"] is True
