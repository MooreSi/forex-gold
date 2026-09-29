"""The VPS starts its Reversal engine when the Mac asks for "reversal".

Two names for one engine. The engine registry, /api/engines/* and
remote_control call it "reversal"; the sync server's `_sub_engines` (and so
the heartbeat) call it "reversal_engine". Every Start/Stop the Mac forwarded
for it came back "unknown engine: reversal", so on 2026-09-28 the owner could
not turn the VPS's Reversal engine on from anywhere.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import MSG_ENGINE_CONTROL_ACK

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Engine:
    def __init__(self):
        self.is_running = False
        self.calls: list = []

    def start(self):
        self.calls.append("start")
        self.is_running = True

    def stop(self):
        self.calls.append("stop")
        self.is_running = False


@pytest.fixture
def node():
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._breakout_engine = _Engine()
    srv._re_engine = _Engine()
    return srv


async def test_reversal_starts_the_reversal_engine(node):
    ws = _Ws()

    await node._handle_engine_control(ws, {"engine": "reversal", "action": "start"})

    assert node._re_engine.calls == ["start"]
    ack = ws.sent[-1]
    assert ack["type"] == MSG_ENGINE_CONTROL_ACK
    assert "error" not in ack
    assert ack["is_running"] is True


async def test_reversal_stops_it_too(node):
    node._re_engine.is_running = True
    ws = _Ws()

    await node._handle_engine_control(ws, {"engine": "reversal", "action": "stop"})

    assert node._re_engine.calls == ["stop"]
    assert ws.sent[-1]["is_running"] is False


async def test_the_heartbeat_name_still_works(node):
    ws = _Ws()

    await node._handle_engine_control(ws, {"engine": "reversal_engine", "action": "start"})

    assert node._re_engine.calls == ["start"]


async def test_an_unknown_name_is_still_refused(node):
    """Negative control: the alias is one name, not a fall-through."""
    ws = _Ws()

    await node._handle_engine_control(ws, {"engine": "reversal_eng", "action": "start"})

    assert node._re_engine.calls == []
    assert node._breakout_engine.calls == []
    assert "unknown engine" in ws.sent[-1]["error"]
