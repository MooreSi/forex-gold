"""Write off the VPS's unconfirmed placeholders from the Mac (owner, 2026-09-28).

Settings > Remote Node shows "Open positions 0 (+2 unconfirmed: in the VPS
database, no broker ticket)". Those rows hold trade slots on the VPS, and the
only place they could be cleared was the VPS itself. This asks the VPS to run
`write_off_unconfirmed` against its own database and broker, and brings the
answer back.

Nothing here closes anything: the repair function and the sockets are
recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss
from backend.src.services.positions import core_template_placeholder_repair as repair

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Runtime:
    def __init__(self):
        self._bridge = object()


def _vps(runtime):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._main_engine = runtime
    return srv


@pytest.fixture
def writes(monkeypatch):
    calls: list = []

    async def _write_off(bridge):
        calls.append(bridge)
        return {"written_off": ["1f5801a6-4450-41", "3041d252-f618-4a"],
                "kept": [], "error": None}
    monkeypatch.setattr(repair, "write_off_unconfirmed", _write_off)
    return calls


class TestTheVpsSide:
    async def test_it_writes_off_against_its_own_bridge_and_says_what(self, writes):
        runtime, ws = _Runtime(), _Ws()

        await _vps(runtime)._dispatch(ws, {"type": P.MSG_WRITE_OFF_UNCONFIRMED})

        assert writes == [runtime._bridge]
        assert ws.sent[0]["type"] == P.MSG_WRITE_OFF_UNCONFIRMED_ACK
        assert ws.sent[0]["written_off"] == ["1f5801a6-4450-41", "3041d252-f618-4a"]

    async def test_no_runtime_writes_off_nothing_and_says_why(self, writes):
        ws = _Ws()

        await _vps(None)._dispatch(ws, {"type": P.MSG_WRITE_OFF_UNCONFIRMED})

        assert writes == []
        assert ws.sent[0]["written_off"] == []
        assert ws.sent[0]["error"]

    async def test_a_write_off_that_raises_is_still_answered(self, monkeypatch):
        async def _boom(bridge):
            raise RuntimeError("db locked")
        monkeypatch.setattr(repair, "write_off_unconfirmed", _boom)
        ws = _Ws()

        await _vps(_Runtime())._dispatch(ws, {"type": P.MSG_WRITE_OFF_UNCONFIRMED})

        assert ws.sent[0]["written_off"] == []
        assert "db locked" in ws.sent[0]["error"]


class _MacWs:
    def __init__(self, client, reply=None):
        self.client = client
        self.reply = reply
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_WRITE_OFF_UNCONFIRMED:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(self.reply)))


def _mac(reply=None, connected=True):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply)
    return cli


class TestTheMacSide:
    async def test_it_asks_and_returns_the_vps_answer(self):
        ack = {"type": P.MSG_WRITE_OFF_UNCONFIRMED_ACK,
               "written_off": ["1f5801a6-4450-41"], "kept": [], "error": None}
        cli = _mac(reply=ack)

        got = await cli.request_peer_write_off(timeout=2)

        assert cli._ws.sent == [{"type": P.MSG_WRITE_OFF_UNCONFIRMED}]
        assert got["written_off"] == ["1f5801a6-4450-41"]

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)

        with pytest.raises(ConnectionError):
            await cli.request_peer_write_off(timeout=2)
        assert cli._ws.sent == []

    async def test_a_vps_that_never_answers_times_out(self):
        cli = _mac(reply=None)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_write_off(timeout=0.2)
