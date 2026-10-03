"""A Remote-mode Start/Stop has to reach the VPS engine the button names.

The two ends of the link spell Reversal differently. The Signal Generator tab
and the engine registry call it "reversal"; the VPS's `SyncServer._sub_engines`
and every other message on the wire (heartbeat `engines`, stood-down lists,
signal_gen_stats, the MSG_ENGINE_CONTROL comment in protocol.py) call it
"reversal_engine". Forwarded unchanged, a Remote-mode Start for Reversal was
acked with `unknown engine: reversal` and the tab said "The remote node
refused" -- found by reading the code, 2026-09-28.

So these drive the REAL names through the real pieces: the router, the
controller, `remote_control`, the real `SyncClient.send_engine_control`, and
the real `SyncServer._handle_engine_control` with its real `_sub_engines`
table. Only the socket and the engines are stand-ins: the "socket" hands the
client's message straight to the server and the server's ack straight back.

The wire key is NOT renamed. An older VPS knows only "reversal_engine", so the
translation happens on this side; a name neither side knows is still refused,
with nothing started.

Nothing here reaches a network, a broker or a database.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster import remote_control as rc
from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync import server as sync_server
from backend.src.services.cluster.sync.protocol import CONN_CONNECTED


class _Engine:
    def __init__(self) -> None:
        self.is_running = False
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        self.is_running = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False


class _ServerSide:
    """The VPS end of the socket: records what the server sends back."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class _Link:
    """The Mac end of the socket, wired directly to a real SyncServer."""

    def __init__(self, client, server) -> None:
        self._client = client
        self._server = server
        self.on_the_wire: list[dict] = []

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.on_the_wire.append(msg)
        vps = _ServerSide()
        await self._server._dispatch(vps, msg)
        for ack in vps.sent:
            await self._client._dispatch(ack)


@pytest.fixture
def paired(monkeypatch):
    """This node in Remote mode, linked to a VPS running the real server code."""
    vps_engines = {"breakout": _Engine(), "bounce": None, "reversal": _Engine()}
    server = sync_server.SyncServer(
        breakout_engine=vps_engines["breakout"],
        bounce_engine=vps_engines["bounce"],
        re_engine=vps_engines["reversal"],
    )

    # A bare client: __init__ reads pending stores from the database, and the
    # only state send_engine_control and the ack dispatch touch is set here.
    client = sync_client.SyncClient.__new__(sync_client.SyncClient)
    client.conn_state = CONN_CONNECTED
    client._engine_control_ack_event = asyncio.Event()
    client._last_engine_control_ack = {}
    link = _Link(client, server)
    client._ws = link

    monkeypatch.setattr(rc._facade, "_is_remote_active", lambda: True)
    monkeypatch.setattr(rc._facade, "_is_centralized_remote_mode", lambda: False)
    monkeypatch.setattr(rc._client, "get_instance", lambda: client)
    return {"engines": vps_engines, "link": link, "server": server}


def test_the_real_server_table_is_what_this_test_is_aimed_at(paired):
    """Negative control on the fixture: if the VPS's keys ever change, the
    tests below would be aimed at a table that no longer exists."""
    assert set(paired["server"]._sub_engines()) == {
        "breakout", "bounce", "reversal_engine"}


@pytest.mark.parametrize("running,expected", [(True, "start"), (False, "stop")])
def test_reversal_start_and_stop_reach_the_vps_reversal_engine(
    make_client, paired, running, expected,
):
    reversal = paired["engines"]["reversal"]
    reversal.is_running = not running

    r = make_client().post("/api/engines/running",
                           json={"engine": "reversal", "running": running})

    assert r.status_code == 200, r.text
    assert r.json()["where"] == "remote"
    assert r.json()["running"] is running
    assert reversal.calls == [expected]
    assert paired["engines"]["breakout"].calls == []


def test_the_wire_carries_the_name_the_vps_has_always_used(make_client, paired):
    """An older VPS knows only "reversal_engine". Renaming the wire key would
    break every paired peer that has not been upgraded."""
    make_client().post("/api/engines/running",
                       json={"engine": "reversal", "running": True})

    assert [m["engine"] for m in paired["link"].on_the_wire] == ["reversal_engine"]


def test_breakout_is_sent_under_its_own_name(make_client, paired):
    """The translation is for Reversal only; Breakout was always the same on
    both sides and must stay that way."""
    r = make_client().post("/api/engines/running",
                           json={"engine": "breakout", "running": True})

    assert r.status_code == 200, r.text
    assert [m["engine"] for m in paired["link"].on_the_wire] == ["breakout"]
    assert paired["engines"]["breakout"].calls == ["start"]


def test_a_name_the_vps_does_not_have_is_refused_and_nothing_starts(paired):
    """Past the router's own check (a peer on another build may know fewer
    engines than this one): the VPS says no, the operator reads it, and no
    engine on either side changes."""
    with pytest.raises(rc.RemoteControlFailed) as exc:
        asyncio.run(rc.set_engine_running("teapot", True))

    assert "unknown engine" in str(exc.value)
    assert all(e.calls == [] for e in paired["engines"].values() if e is not None)
