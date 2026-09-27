"""Restart the VPS from the Mac's Settings > Remote Node (owner, 2026-09-26).

"there also needs to be a way to restart the vps within the settings tab to
avoid having to login into the vps". Until now the only ways were Telegram
(/restartapp) and Remote Desktop (simon-handover/043, item 4).

The VPS restarts the way /restartapp does -- `TradingRuntime.restart_app`,
which persists the bot offset, hands a supervised install back to its
launcher, and ends a headless process that has no web server to stop -- so
this adds a way to ASK, not a second way to restart.

Nothing here restarts anything: the runtime and the socket are recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Runtime:
    def __init__(self, reply="Restarting app in 5 seconds — reconnect your browser shortly."):
        self.reply = reply
        self.restarts: list[list] = []

    async def restart_app(self, args):
        self.restarts.append(args)
        return self.reply


def _vps(runtime):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._main_engine = runtime
    return srv


class TestTheVpsSide:
    async def test_it_restarts_the_way_restartapp_does_and_says_so(self):
        runtime, ws = _Runtime(), _Ws()

        await _vps(runtime)._dispatch(ws, {"type": P.MSG_RESTART_NODE})

        assert runtime.restarts == [[]]
        assert ws.sent == [{"type": P.MSG_RESTART_NODE_ACK, "ok": True,
                            "note": runtime.reply}]

    async def test_a_failed_restart_is_reported_not_claimed(self):
        runtime, ws = _Runtime(reply="Restart failed: no run.py"), _Ws()

        await _vps(runtime)._dispatch(ws, {"type": P.MSG_RESTART_NODE})

        assert ws.sent[0]["ok"] is False
        assert ws.sent[0]["note"] == "Restart failed: no run.py"

    async def test_no_runtime_means_no_restart_and_a_reason(self):
        ws = _Ws()

        await _vps(None)._dispatch(ws, {"type": P.MSG_RESTART_NODE})

        assert ws.sent[0]["type"] == P.MSG_RESTART_NODE_ACK
        assert ws.sent[0]["ok"] is False
        assert ws.sent[0]["note"]

    async def test_a_restart_that_raises_is_still_answered(self):
        class _Broken:
            async def restart_app(self, args):
                raise RuntimeError("boom")
        ws = _Ws()

        await _vps(_Broken())._dispatch(ws, {"type": P.MSG_RESTART_NODE})

        assert ws.sent[0]["ok"] is False
        assert "boom" in ws.sent[0]["note"]


class _MacWs:
    """The Mac's socket: records what it sends and answers the way a VPS would."""

    def __init__(self, client, reply=None):
        self.client = client
        self.reply = reply
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_RESTART_NODE:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(self.reply)))


def _mac(reply=None, connected=True):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply)
    return cli


class TestTheMacSide:
    async def test_it_asks_and_returns_the_vps_answer(self):
        ack = {"type": P.MSG_RESTART_NODE_ACK, "ok": True, "note": "Restarting"}
        cli = _mac(reply=ack)

        got = await cli.request_peer_restart(timeout=2)

        assert cli._ws.sent == [{"type": P.MSG_RESTART_NODE}]
        assert got["ok"] is True and got["note"] == "Restarting"

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)

        with pytest.raises(ConnectionError):
            await cli.request_peer_restart(timeout=2)
        assert cli._ws.sent == []

    async def test_a_vps_that_never_answers_times_out(self):
        """An older VPS has no handler and says nothing back."""
        cli = _mac(reply=None)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_restart(timeout=0.2)
