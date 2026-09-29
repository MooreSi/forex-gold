"""POST /api/trading/remote/resume-all: the header's Resume, sent to the VPS.

A route of its own, like the remote close, so the Mac's own `/resume-all` is
never quietly turned into a forward (or the other way round).
"""
from __future__ import annotations

from backend.src.api.routers import trading_status as router_mod


def test_it_forwards_and_returns_what_the_vps_cleared(make_client, monkeypatch):
    calls = []

    async def _resume():
        calls.append(1)
        return {"cleared": ["circuit_breaker"], "where": "remote"}

    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_on_vps", _resume)

    body = make_client().post("/api/trading/remote/resume-all").json()

    assert calls == [1]
    assert body["cleared"] == ["circuit_breaker"]


def test_a_vps_refusal_reaches_the_operator_as_a_refusal(make_client, monkeypatch):
    async def _refused():
        raise router_mod.status_ctl.RemoteControlFailed(
            "The remote node could not be reached (x). Nothing was resumed.")

    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_on_vps", _refused)

    res = make_client().post("/api/trading/remote/resume-all")

    assert 400 <= res.status_code < 500
    assert "Nothing was resumed" in res.text


def test_it_is_not_reachable_by_GET(make_client):
    assert make_client().get("/api/trading/remote/resume-all").status_code == 405
