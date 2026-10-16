""""Resume anyway (ignore today's goal)" for a badge that reports the VPS
(owner, 2026-10-16).

The VPS's goal halt is in the VPS's database, so the Mac sends the same
MSG_RESUME_TRADING with `past_goal: true` and the VPS runs its own
`trading_status.resume_all(past_goal=True)`. A plain Resume still sends no
flag, and the VPS still calls `resume_all()` with no argument, so an older
Mac talking to this VPS is unchanged. An older VPS ignores the flag and keeps
the goal (its reply carries `kept`), which is the safe direction.

**Nothing here reaches a broker.** `resume_all` is a recorder and the sockets
are recorders.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster import remote_control as rc
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
from backend.src.services.risk import trading_status as ts


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
class TestTheVpsSide:
    async def test_the_flag_reaches_resume_all(self, fresh_db, monkeypatch):
        calls = []
        monkeypatch.setattr(ts, "resume_all",
                            lambda **kw: calls.append(kw) or {"cleared": ["manual_pause"]})
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_RESUME_TRADING, "req_id": "r1", "past_goal": True})

        assert calls == [{"past_goal": True}]
        assert ws.sent[0]["result"] == {"cleared": ["manual_pause"]}

    async def test_no_flag_is_a_plain_resume(self, fresh_db, monkeypatch):
        calls = []
        monkeypatch.setattr(ts, "resume_all",
                            lambda **kw: calls.append(kw) or {"cleared": []})

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            _Ws(), {"type": P.MSG_RESUME_TRADING, "req_id": "r2"})

        assert calls == [{}]


class _FakeClient:
    def __init__(self):
        self.kwargs: list[dict] = []

    async def request_peer_resume_trading(self, **kw):
        self.kwargs.append(kw)
        return {"result": {"cleared": ["manual_pause"]}}


@pytest.mark.asyncio
class TestTheMacSide:
    async def test_past_goal_is_sent_to_the_vps(self, monkeypatch):
        fake = _FakeClient()
        monkeypatch.setattr(rc._client, "get_instance", lambda: fake)
        local = []
        monkeypatch.setattr(ts, "resume_all", lambda **kw: local.append(kw))

        got = await rc.resume_trading_on_peer(past_goal=True)

        assert fake.kwargs == [{"past_goal": True}]
        assert got["where"] == "remote"
        assert local == [], "the Mac's own holds must not be touched"

    async def test_the_message_carries_the_flag(self):
        cli = sc.SyncClient.__new__(sc.SyncClient)
        cli.conn_state = P.CONN_CONNECTED
        cli._ws = _Ws()
        with pytest.raises(Exception):
            await cli.request_peer_resume_trading(timeout=0.05, past_goal=True)
        assert cli._ws.sent[0]["past_goal"] is True

    async def test_a_plain_resume_sends_no_flag(self):
        cli = sc.SyncClient.__new__(sc.SyncClient)
        cli.conn_state = P.CONN_CONNECTED
        cli._ws = _Ws()
        with pytest.raises(Exception):
            await cli.request_peer_resume_trading(timeout=0.05)
        assert "past_goal" not in cli._ws.sent[0]


_GOAL_HALTED = {
    "state": "halted", "label": "Goal Achieved Paused until 17 Oct 00:00",
    "detail": "Daily goal secured: +$29.57 today vs a goal of $10.00",
    "until": 1_760_655_600.0, "resume_ts": None, "can_resume": False,
    "can_resume_past_goal": True,
}


@pytest.fixture
def mac(fresh_db, monkeypatch):
    fresh_db.set_app_config("sync_remote_host", "203.0.113.7")
    fresh_db.set_app_config("active_trader", TRADER_REMOTE_VPS)
    cli = sc.get_instance()
    monkeypatch.setattr(cli, "conn_state", "connected")
    monkeypatch.setattr(cli, "remote_status", {"trading_status": dict(_GOAL_HALTED)})
    return cli


class TestTheBadge:
    def test_a_goal_paused_vps_offers_resume_past_goal(self, mac):
        b = ts.badge()
        assert b["can_resume_on_vps"] is False
        assert b["can_resume_past_goal_on_vps"] is True

    def test_an_older_vps_that_does_not_say_is_not_offered_it(self, mac):
        old = {k: v for k, v in _GOAL_HALTED.items() if k != "can_resume_past_goal"}
        mac.remote_status = {"trading_status": old}
        assert ts.badge()["can_resume_past_goal_on_vps"] is False
