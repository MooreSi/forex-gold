"""POST /api/trading/resume-past-goal and /remote/resume-past-goal:
"Resume anyway (ignore today's goal)" (owner, 2026-10-16).

Routes of their own, so a plain Resume can never become the override by a
missing or defaulted field, and the Mac's own never becomes a forward.
Nothing here reaches a broker: the controller calls are recorders.
"""
from __future__ import annotations

from backend.src.api.routers import trading_status as router_mod


def test_local_override_calls_the_past_goal_resume(make_client, monkeypatch):
    calls = []
    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_past_goal",
                        lambda: calls.append(1) or {"cleared": ["manual_pause"]})
    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_all",
                        lambda: calls.append("plain") or {"cleared": []})

    body = make_client().post("/api/trading/resume-past-goal").json()

    assert calls == [1]
    assert body["cleared"] == ["manual_pause"]


def test_plain_resume_all_does_not_use_the_override(make_client, monkeypatch):
    calls = []
    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_past_goal",
                        lambda: calls.append("override") or {"cleared": []})
    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_all",
                        lambda: calls.append("plain") or {"cleared": []})

    make_client().post("/api/trading/resume-all")

    assert calls == ["plain"]


def test_the_vps_override_forwards(make_client, monkeypatch):
    calls = []

    async def _resume():
        calls.append(1)
        return {"cleared": ["manual_pause"], "where": "remote"}

    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_past_goal_on_vps", _resume)

    body = make_client().post("/api/trading/remote/resume-past-goal").json()

    assert calls == [1]
    assert body["where"] == "remote"


def test_a_vps_refusal_reaches_the_operator_as_a_refusal(make_client, monkeypatch):
    async def _refused():
        raise router_mod.status_ctl.RemoteControlFailed(
            "The remote node could not be reached (x). Nothing was resumed.")

    monkeypatch.setattr(router_mod.status_ctl, "resume_trading_past_goal_on_vps", _refused)

    res = make_client().post("/api/trading/remote/resume-past-goal")

    assert 400 <= res.status_code < 500
    assert "Nothing was resumed" in res.text
