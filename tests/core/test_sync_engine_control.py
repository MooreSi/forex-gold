"""Starting and stopping this node's signal engines from the other one.

When the Mac is in Remote mode its own engines are stood down, so its Signal
Generator buttons have to act on the VPS's engines instead — otherwise they
drive a stood-down copy that does nothing while looking like it worked. That is
what `_handle_engine_control` is for, and starting an engine means it can
generate signals that get traded.

The properties that matter are about what must NOT happen, and about the reply:

  * An unknown engine or an unknown action must do nothing. There is no
    fall-through, and a router that defaulted to `start` would turn a
    malformed message into a running generator.
  * `set_ai_eval` writes a risk setting, so the engine name is mapped through a
    two-entry table rather than interpolated. Anything else is an error, not a
    write.
  * **Every path replies.** The Mac's button sits with a spinner on it; a
    branch that returns without an ack leaves the operator unable to tell
    "refused" from "not delivered".
  * The ack reports the engine's ACTUAL running state, not the one that was
    asked for. Echoing the request back would make a failed start look like a
    success.
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
    def __init__(self, running=False, raises=False):
        self.is_running = running
        self._raises = raises
        self.calls: list = []

    def start(self):
        self.calls.append("start")
        if self._raises:
            raise RuntimeError("engine failed to start")
        self.is_running = True

    def stop(self):
        self.calls.append("stop")
        self.is_running = False

    async def _run_cycle(self):
        self.calls.append("run_now")


@pytest.fixture
def settings_writes(monkeypatch):
    writes: list = []
    from backend.src.db import database as _db
    monkeypatch.setattr(_db, "update_risk_settings",
                        lambda fields, **kw: writes.append(fields))
    return writes


@pytest.fixture
def node(monkeypatch):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv.engines = {"bounce": _Engine(), "breakout": _Engine(),
                   "reversal_engine": _Engine()}
    monkeypatch.setattr(ss.SyncServer, "_sub_engines", lambda _s: srv.engines)
    return srv


async def _control(node, ws, **msg):
    await node._handle_engine_control(ws, msg)
    return ws.sent[-1] if ws.sent else None


class TestEveryPathReplies:
    """The Mac drives these from a screen with a spinner on it."""

    @pytest.mark.parametrize("msg", [
        {"engine": "bounce", "action": "start"},
        {"engine": "bounce", "action": "stop"},
        {"engine": "bounce", "action": "run_now"},
        {"engine": "nonsense", "action": "start"},
        {"engine": "bounce", "action": "nonsense"},
        {},
    ])
    async def test_it_acks(self, node, msg, settings_writes):
        ws = _Ws()

        await node._handle_engine_control(ws, msg)

        assert [m["type"] for m in ws.sent] == [MSG_ENGINE_CONTROL_ACK]


class TestNothingHappensOnAMalformedMessage:

    async def test_an_unknown_engine_is_an_error_not_an_action(self, node):
        ack = await _control(node, _Ws(), engine="not_an_engine", action="start")

        assert "unknown engine" in ack["error"]
        assert all(e.calls == [] for e in node.engines.values())

    async def test_an_unknown_action_is_an_error_not_a_start(self, node):
        """A router that fell through to `start` would turn a malformed message
        into a running signal generator."""
        ack = await _control(node, _Ws(), engine="bounce", action="frobnicate")

        assert "unknown action" in ack["error"]
        assert node.engines["bounce"].calls == []
        assert node.engines["bounce"].is_running is False

    async def test_an_empty_message_starts_nothing(self, node):
        ack = await _control(node, _Ws())

        assert ack.get("error")
        assert all(e.calls == [] for e in node.engines.values())


class TestTheActionsThatDoWork:
    """Positive controls: a handler that errored on everything would satisfy
    every test above."""

    async def test_start_starts(self, node):
        ack = await _control(node, _Ws(), engine="bounce", action="start")

        assert node.engines["bounce"].calls == ["start"]
        assert ack.get("error") is None
        assert ack["is_running"] is True

    async def test_stop_stops(self, node):
        node.engines["bounce"].is_running = True

        ack = await _control(node, _Ws(), engine="bounce", action="stop")

        assert node.engines["bounce"].calls == ["stop"]
        assert ack["is_running"] is False

    async def test_run_now_runs_one_cycle(self, node):
        ack = await _control(node, _Ws(), engine="reversal_engine",
                             action="run_now")

        assert node.engines["reversal_engine"].calls == ["run_now"]
        assert ack.get("error") is None


class TestTheAckReportsRealityNotTheRequest:

    async def test_a_failed_start_is_reported_as_not_running(self, node):
        """Echoing the requested action back would make a failed start look
        like a success, and the Mac's button would go green."""
        node.engines["bounce"] = _Engine(raises=True)

        ack = await _control(node, _Ws(), engine="bounce", action="start")

        assert "engine failed to start" in ack["error"]
        assert ack["is_running"] is False

    async def test_an_engine_that_raises_does_not_kill_the_connection(self, node):
        node.engines["bounce"] = _Engine(raises=True)
        ws = _Ws()

        await node._handle_engine_control(ws, {"engine": "bounce",
                                               "action": "start"})

        assert ws.sent, "the exception escaped instead of becoming an ack"


class TestSetAiEvalWritesOnlyItsTwoKnownKeys:
    """This one writes a risk setting rather than touching an engine, so the
    engine name is mapped through a table instead of being interpolated."""

    @pytest.mark.parametrize("engine,key", [
        ("bounce", "sg_claude_eval_enabled"),
        ("breakout", "bo_claude_eval_enabled"),
    ])
    async def test_it_maps_the_engine_to_its_own_flag(self, node,
                                                      settings_writes,
                                                      engine, key):
        await _control(node, _Ws(), engine=engine, action="set_ai_eval",
                       enabled=True)

        assert settings_writes == [{key: 1}]

    async def test_disabling_writes_zero_not_absence(self, node,
                                                     settings_writes):
        await _control(node, _Ws(), engine="bounce", action="set_ai_eval",
                       enabled=False)

        assert settings_writes == [{"sg_claude_eval_enabled": 0}]

    async def test_an_engine_with_no_flag_writes_NOTHING(self, node,
                                                         settings_writes):
        """reversal_engine is a real engine with no AI-eval flag. It must be an
        error rather than a write of some interpolated key name."""
        ack = await _control(node, _Ws(), engine="reversal_engine",
                             action="set_ai_eval", enabled=True)

        assert settings_writes == []
        assert "not supported" in ack["error"]
