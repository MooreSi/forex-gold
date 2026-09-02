"""The three loops that keep the sync link alive and current.

Each is a `while self.conn_state == CONN_CONNECTED` around one send and a
sleep, and each carries the same two properties:

  * **It stops when the link goes.** A loop that keeps running after a
    disconnect writes to a dead socket for ever, and three of them do it in
    parallel.
  * **A failed send breaks out rather than raising.** These run as bare
    `asyncio.create_task`, so an escaping exception becomes an unretrieved
    task exception -- logged once at process exit, invisible while running,
    and the loop is gone either way with nothing saying so.

The liveness ping is the one that matters most: the VPS's own watchdog uses
"last message from the Mac" to decide the link is dead, so a ping loop that
stops silently makes a healthy link look broken. It also carries the Mac's
clock offset, which is what keeps the VPS's trading clock correct.

No sockets and no waiting: the sleep is replaced.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync.protocol import (
    MSG_AI_RECOVERED_PULL, MSG_LEDGER_PULL, MSG_PING,
)

pytestmark = pytest.mark.asyncio

# Captured at import, before any fixture patches asyncio.sleep. Taking it
# inside a test gets whatever the fixture already installed -- which
# disconnects -- so the loop under test ended after one iteration and the
# assertion failed on working code.
_REAL_SLEEP = asyncio.sleep

LOOPS = [
    ("_liveness_ping_loop", MSG_PING),
    ("_ledger_pull_loop", MSG_LEDGER_PULL),
    ("_ai_recovered_pull_loop", MSG_AI_RECOVERED_PULL),
]
IDS = [name for name, _t in LOOPS]


class _Ws:
    def __init__(self, fails_after=None):
        self.sent: list = []
        self._fails_after = fails_after

    async def send(self, raw):
        if self._fails_after is not None and len(self.sent) >= self._fails_after:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(raw))

    def types(self):
        return [m.get("type") for m in self.sent]


async def _run(coro):
    """Run a loop under a hard timeout.

    Every call goes through this. A loop that stops respecting conn_state
    does not fail these tests, it HANGS them -- and a mutation run that hangs
    is killed by its own timeout with the mutation still in the file. That
    happened on 2026-09-02 and left `while True:` in _ledger_pull_loop.
    """
    return await asyncio.wait_for(coro, timeout=2.0)


@pytest.fixture
def node(monkeypatch):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = sc.CONN_CONNECTED
    cli._ws = _Ws()

    # Each sleep disconnects, so every loop runs exactly one iteration and
    # then exits through its own condition rather than being cancelled.
    #
    # It must also YIELD. A coroutine that awaits nothing never returns
    # control to the event loop, so a runaway `while True:` spins without
    # `wait_for` ever getting the chance to time it out -- the test hangs
    # instead of failing, and on 2026-09-02 that killed a mutation run
    # mid-flight and left the mutation in the source.
    async def _sleep(_s):
        cli.conn_state = sc.CONN_DISCONNECTED
        await _REAL_SLEEP(0)
    monkeypatch.setattr(sc.asyncio, "sleep", _sleep)
    return cli


@pytest.mark.parametrize("name,msg_type", LOOPS, ids=IDS)
class TestEveryLoop:
    async def test_it_sends_its_message(self, node, name, msg_type):
        await _run(getattr(node, name)())

        assert msg_type in node._ws.types()

    async def test_it_stops_when_the_link_goes(self, node, name, msg_type):
        """The sleep in the fixture drops the connection, so a loop that
        respects conn_state runs once. One that does not runs for ever and
        this test hangs rather than fails -- which is why the fixture's sleep
        never actually waits."""
        await _run(getattr(node, name)())

        assert len(node._ws.sent) == 1

    async def test_it_does_not_start_when_already_disconnected(
        self, node, name, msg_type,
    ):
        node.conn_state = sc.CONN_DISCONNECTED

        await _run(getattr(node, name)())

        assert node._ws.sent == []

    async def test_a_failed_send_breaks_out_quietly(self, node, name, msg_type):
        """These run as bare create_task. An escaping exception is an
        unretrieved task exception: invisible while running, and the loop is
        gone either way with nothing saying so."""
        node._ws = _Ws(fails_after=0)

        await _run(getattr(node, name)())

    async def test_no_socket_is_survivable(self, node, name, msg_type):
        """conn_state and _ws are set in different places; a loop that
        assumes both are consistent raises on the gap between them."""
        node._ws = None

        await _run(getattr(node, name)())

    async def test_a_momentarily_missing_socket_does_not_end_the_loop(
        self, node, monkeypatch, name, msg_type,
    ):
        """`_ws` is None for a moment during a reconnect while conn_state is
        still CONNECTED. Skipping that iteration and sending on the next is
        the difference between a brief gap and a loop that never sends again.

        Without the guard the AttributeError is caught by the same handler
        and breaks out -- so a test that only checks "it did not raise"
        passes either way. Mutation found that; this one watches the SECOND
        iteration.
        """
        ws = _Ws()
        calls = {"n": 0}

        async def _sleep(_s):
            calls["n"] += 1
            if calls["n"] == 1:
                node._ws = ws              # the socket comes back
            else:
                node.conn_state = sc.CONN_DISCONNECTED
            await _REAL_SLEEP(0)
        monkeypatch.setattr(sc.asyncio, "sleep", _sleep)
        node._ws = None

        await _run(getattr(node, name)())

        assert msg_type in ws.types(), "the loop gave up on a momentary gap"


class TestThePingCarriesTheClock:
    async def test_it_reports_this_machine_s_offset(self, node, monkeypatch):
        """The VPS adopts this to keep its trading clock right -- see
        test_clock_offset_sync.py. A ping without it silently leaves the VPS
        on its own timezone."""
        monkeypatch.setattr(sc._clock, "effective_offset_minutes", lambda: 330)

        await _run(node._liveness_ping_loop())

        assert node._ws.sent[0]["clock_offset_min"] == 330

    async def test_a_clock_failure_does_not_kill_the_heartbeat(
        self, node, monkeypatch,
    ):
        """The VPS's watchdog uses "last message from the Mac" to decide the
        link is dead. Losing the ping over a clock detail would make a
        healthy link look broken."""
        def _boom():
            raise RuntimeError("no settings")
        monkeypatch.setattr(sc._clock, "effective_offset_minutes", _boom)

        await _run(node._liveness_ping_loop())

        assert MSG_PING in node._ws.types(), (
            "the heartbeat stopped because the clock could not be read"
        )
