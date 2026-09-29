"""Resume the VPS's trading from the Mac's header (owner, 2026-09-29).

"i should have a resume trading button on the popup as if it was local". With
the VPS as the active trader the header badge is the VPS's, and a breaker
tripped there could only be cleared by logging in to the VPS. The Mac now asks
and the VPS runs its own `trading_status.resume_all()`: the same function
`POST /api/trading/resume-all` runs on a node that trades for itself.

**Nothing here reaches a broker.** `resume_all` is replaced by a recorder on
the VPS side and the sockets are recorders. A resume places no order; it
clears a hold.

What each layer must never do:

  * the Mac must never resume ITSELF instead: its holds guard no orders, so a
    local fallback would report success and change nothing that trades;
  * the Mac must never call an unanswered request "nothing resumed": the VPS
    may have resumed and lost the reply;
  * the badge must offer this only when the VPS itself says a Resume would
    change something.
"""
from __future__ import annotations

import asyncio
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


# ── the VPS side ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTheVpsSide:
    async def test_it_runs_its_own_resume_all_and_answers(self, fresh_db, monkeypatch):
        calls = []
        monkeypatch.setattr(ts, "resume_all", lambda: calls.append(1) or {
            "cleared": ["circuit_breaker"], "status": {"state": "ok"}})
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_RESUME_TRADING, "req_id": "r1"})

        assert calls == [1]
        assert ws.sent == [{"type": P.MSG_RESUME_TRADING_ACK, "req_id": "r1",
                            "result": {"cleared": ["circuit_breaker"],
                                       "status": {"state": "ok"}}}]

    async def test_a_failure_comes_back_as_an_error(self, fresh_db, monkeypatch):
        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(ts, "resume_all", _boom)
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_RESUME_TRADING, "req_id": "r2"})

        assert ws.sent[0]["req_id"] == "r2"
        assert "database is locked" in ws.sent[0]["error"]
        assert "result" not in ws.sent[0]

    async def test_it_is_not_the_stand_down_resume(self):
        """MSG_RESUME hands control back to the VPS. Routing a trading
        resume into it would change who trades."""
        assert P.MSG_RESUME_TRADING != P.MSG_RESUME
        assert P.MSG_RESUME_TRADING_ACK != P.MSG_RESUME_ACK


# ── the Mac side ─────────────────────────────────────────────────────────────

class _MacWs:
    def __init__(self, client, reply=None, wrong_id=False):
        self.client, self.reply, self.wrong_id = client, reply, wrong_id
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_RESUME_TRADING:
            ack = {**self.reply, "type": P.MSG_RESUME_TRADING_ACK,
                   "req_id": "someone-else" if self.wrong_id else msg["req_id"]}
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(ack)))


def _mac(reply=None, connected=True, wrong_id=False):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply, wrong_id)
    return cli


@pytest.mark.asyncio
class TestTheMacSide:
    async def test_it_asks_and_returns_the_answer(self):
        cli = _mac(reply={"result": {"cleared": ["circuit_breaker"]}})

        got = await cli.request_peer_resume_trading(timeout=2)

        assert cli._ws.sent[0]["type"] == P.MSG_RESUME_TRADING
        assert cli._ws.sent[0]["req_id"]
        assert got["result"] == {"cleared": ["circuit_breaker"]}

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)

        with pytest.raises(ConnectionError):
            await cli.request_peer_resume_trading(timeout=2)
        assert cli._ws.sent == []

    async def test_an_answer_to_another_request_is_not_taken_as_this_ones(self):
        cli = _mac(reply={"result": {"cleared": []}}, wrong_id=True)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_resume_trading(timeout=0.3)


# ── what the operator is told ────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.calls = 0

    async def request_peer_resume_trading(self, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture
def peer(monkeypatch):
    holder = {"client": _FakeClient(reply={"result": {
        "cleared": ["circuit_breaker"], "status": {"state": "ok"}}})}
    monkeypatch.setattr(rc._client, "get_instance", lambda: holder["client"])
    return holder


@pytest.mark.asyncio
class TestResumeOnPeer:
    async def test_success_returns_what_the_vps_cleared(self, peer, monkeypatch):
        local = []
        monkeypatch.setattr(ts, "resume_all", lambda: local.append(1))

        got = await rc.resume_trading_on_peer()

        assert got["cleared"] == ["circuit_breaker"]
        assert got["where"] == "remote"
        assert local == [], "the Mac's own holds must not be touched"

    async def test_a_refusal_is_the_vps_reason(self, peer):
        peer["client"] = _FakeClient(reply={"error": "database is locked"})

        with pytest.raises(rc.RemoteControlFailed, match="database is locked"):
            await rc.resume_trading_on_peer()

    async def test_unreachable_says_nothing_changed_and_does_not_fall_back(
            self, peer, monkeypatch):
        local = []
        monkeypatch.setattr(ts, "resume_all", lambda: local.append(1))
        peer["client"] = _FakeClient(raises=ConnectionError("not connected to VPS"))

        with pytest.raises(rc.RemoteControlFailed, match="Nothing was resumed"):
            await rc.resume_trading_on_peer()
        assert local == []

    async def test_no_answer_never_claims_nothing_was_resumed(self, peer):
        peer["client"] = _FakeClient(raises=asyncio.TimeoutError())

        with pytest.raises(rc.RemoteControlFailed) as exc:
            await rc.resume_trading_on_peer()
        assert "Nothing was resumed" not in str(exc.value)
        assert "may have resumed" in str(exc.value)


# ── the badge offers it ──────────────────────────────────────────────────────

_NOW = 1_758_000_000.0
_VPS_HALTED = {
    "state": "halted", "label": "Trading Paused until 16 Sep 06:15",
    "detail": "Circuit breaker active (3 consecutive losses)",
    "until": _NOW + 900, "resume_ts": None, "can_resume": True,
}


@pytest.fixture
def mac(fresh_db, monkeypatch):
    fresh_db.set_app_config("sync_remote_host", "203.0.113.7")
    fresh_db.set_app_config("active_trader", TRADER_REMOTE_VPS)
    cli = sc.get_instance()
    monkeypatch.setattr(cli, "conn_state", "connected")
    monkeypatch.setattr(cli, "remote_status", {"trading_status": dict(_VPS_HALTED)})
    return cli


class TestTheBadge:
    def test_a_halted_vps_offers_the_vps_resume(self, mac):
        assert ts.badge()["can_resume_on_vps"] is True

    def test_the_local_resume_is_still_not_offered(self, mac):
        assert ts.badge()["can_resume"] is False

    def test_not_offered_when_the_vps_says_it_would_change_nothing(self, mac):
        mac.remote_status = {"trading_status": {**_VPS_HALTED, "state": "news_blackout",
                                                "can_resume": False}}

        assert ts.badge()["can_resume_on_vps"] is False

    def test_not_offered_with_the_link_down(self, mac):
        mac.conn_state = "disconnected"

        assert not ts.badge().get("can_resume_on_vps")
