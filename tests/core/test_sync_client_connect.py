"""`SyncClient._connect_once`: what happens between opening a socket and trusting it.

`tests/core/test_sync_cert_pinning.py` covers `tls_util` in isolation — whether
a fingerprint is accepted, remembered, refused. It does not cover the thing
bugs/014 was actually about: **that `_connect_once` checks the pin BEFORE it
sends the shared token.** Pinning after the token has left is worth nothing;
the attacker already has it.

So the headline test here does not assert a state or a log line. It asserts
that when the pin is refused, the socket received **no frames at all**.

Also covered, because both are silent when they go wrong:

  * On connect, all four remote snapshots (settings, channel strategy, trading
    schedule, strategy params) are mirrored into the local database. A Mac that
    connects and mirrors nothing shows the operator its own stale copy of the
    VPS's configuration, with no indication.
  * Unconfirmed local changes are re-sent after a reconnect. `propose_settings`
    used to be fire-and-forget, and with this link reconnecting every 15-90s
    for hours, a risk-settings change that never reached the VPS was a
    recurring real failure, not an edge case.

`websockets.connect` is replaced by `monkeypatch.setattr` on the module
attribute — NOT by swapping `sys.modules["websockets"]`, which leaks into other
tests in the same session and produced ten phantom failures the last time it
was tried here.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync import tls_util
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, CONN_REJECTED, MSG_HELLO, MSG_REJECT, MSG_WELCOME, make,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    """Records every frame the client sends, and serves scripted replies."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent: list = []
        self.transport = None

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._replies:
            raise asyncio.TimeoutError("no more replies")
        r = self._replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def __aiter__(self):
        """The connected path ends in `async for raw in ws`. With no frames
        left it stops immediately, which is the "VPS closed the link" case and
        exactly what these tests want after the handshake."""
        return self

    async def __anext__(self):
        if not self._replies:
            raise StopAsyncIteration
        r = self._replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def welcome():
    return make(
        MSG_WELCOME,
        settings={"max_open_trades": 5},
        channel_strategy={"Gold VIP": "scale_out"},
        trading_schedule={"enabled": True, "schedule": {}},
        strategy_params={"scale_out": {"tp1_pct": 50}},
        active_trader="remote_vps",
        node_id="vps-1",
    )


@pytest.fixture
def client(monkeypatch):
    """A SyncClient with its mirrors and TLS replaced, so nothing touches the
    database or a real socket."""
    c = sync_client.SyncClient.__new__(sync_client.SyncClient)
    c.conn_state = "disconnected"
    c.last_error = ""
    c._host, c._port, c._token = "vps.example", 8765, "shared-token"
    c._ws = None
    c._running = True
    c.remote_settings = {}
    c.remote_channel_strategy = {}
    c.remote_trading_schedule = {}
    c.remote_strategy_params = {}
    c._pending_settings = {}
    c._pending_channel_strategy = {}
    c._pending_trading_schedule = None
    c._pending_strategy_params = None

    c.mirrored = {}
    for name in ("settings", "channel_strategy", "trading_schedule",
                 "strategy_params"):
        def _mk(n):
            def _mirror(payload, _n=n):
                c.mirrored[_n] = payload
            return _mirror
        setattr(c, f"_mirror_{name}_locally", _mk(name))

    async def _noop_loop():
        return None
    for _loop in ("_ledger_pull_loop", "_ai_recovered_pull_loop",
                  "_liveness_ping_loop"):
        setattr(c, _loop, _noop_loop)

    monkeypatch.setattr(tls_util, "client_ssl_context", lambda: None)
    monkeypatch.setattr(tls_util, "peer_fingerprint", lambda ws: "AA:BB")
    return c


def _serving(monkeypatch, ws):
    monkeypatch.setattr(websockets, "connect", lambda *a, **kw: ws)


class TestThePinIsCheckedBeforeTheTokenLeaves:
    """bugs/014. The whole point of the fix, asserted the only way that means
    anything: nothing was sent."""

    async def test_a_refused_pin_sends_NOTHING(self, client, monkeypatch):
        monkeypatch.setattr(tls_util, "verify_or_pin",
                            lambda h, f: (False, "fingerprint changed"))
        ws = _Ws([])
        _serving(monkeypatch, ws)

        await client._connect_once()

        assert ws.sent == [], (
            "the shared token was sent to a peer whose certificate was refused"
        )
        assert client.conn_state == CONN_REJECTED
        assert "fingerprint changed" in client.last_error

    async def test_a_refused_pin_does_not_mirror_anything(self, client,
                                                          monkeypatch, welcome):
        monkeypatch.setattr(tls_util, "verify_or_pin",
                            lambda h, f: (False, "fingerprint changed"))
        ws = _Ws([welcome])
        _serving(monkeypatch, ws)

        await client._connect_once()

        assert client.mirrored == {}

    async def test_an_accepted_pin_DOES_send_the_token(self, client,
                                                       monkeypatch, welcome):
        """Positive control. Without it, a client that refused every
        connection would satisfy both tests above."""
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        ws = _Ws([welcome])
        _serving(monkeypatch, ws)

        await client._connect_once()

        assert ws.types() == [MSG_HELLO]
        assert ws.sent[0]["token"] == "shared-token"


class TestTheHandshakeOutcome:

    async def test_a_rejection_from_the_vps_is_recorded_not_ignored(
            self, client, monkeypatch):
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        ws = _Ws([make(MSG_REJECT, reason="bad token")])
        _serving(monkeypatch, ws)

        await client._connect_once()

        assert client.conn_state == CONN_REJECTED
        assert client.last_error == "bad token"
        assert client.mirrored == {}, "a rejected link mirrored VPS state anyway"

    async def test_a_welcome_marks_the_link_connected(self, client, monkeypatch,
                                                      welcome):
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        _serving(monkeypatch, _Ws([welcome]))

        await client._connect_once()

        assert client.conn_state == CONN_CONNECTED


class TestEverythingTheVpsSendsIsMirroredLocally:
    """A Mac that connects and mirrors nothing shows its own stale copy of the
    VPS's configuration, with nothing to say so."""

    @pytest.fixture(autouse=True)
    def _connected(self, client, monkeypatch, welcome):
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        _serving(monkeypatch, _Ws([welcome]))

    @pytest.mark.parametrize("name", [
        "settings", "channel_strategy", "trading_schedule", "strategy_params",
    ])
    async def test_each_snapshot_reaches_the_local_mirror(self, client, name,
                                                          welcome):
        await client._connect_once()

        assert name in client.mirrored, f"the VPS's {name} was never mirrored"
        assert client.mirrored[name] == welcome[name]

    async def test_the_snapshots_are_also_kept_on_the_client(self, client,
                                                             welcome):
        await client._connect_once()

        assert client.remote_settings == welcome["settings"]
        assert client.remote_channel_strategy == welcome["channel_strategy"]
        assert client.remote_trading_schedule == welcome["trading_schedule"]
        assert client.remote_strategy_params == welcome["strategy_params"]


class TestUnconfirmedLocalChangesSurviveAReconnect:
    """`propose_settings` was fire-and-forget. On a link that reconnects every
    15-90s, a risk-settings change made at the wrong moment was silently lost
    and never retried."""

    async def test_pending_settings_are_flushed_on_reconnect(
            self, client, monkeypatch, welcome):
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        _serving(monkeypatch, _Ws([welcome]))
        flushed: list = []

        async def _flush():
            flushed.append("settings")
        client._flush_pending_settings = _flush
        client._pending_settings = {"max_open_trades": 3}

        await client._connect_once()
        await asyncio.sleep(0)      # let the created task run

        assert flushed == ["settings"], "an unconfirmed change was dropped"

    async def test_nothing_is_flushed_when_there_is_nothing_pending(
            self, client, monkeypatch, welcome):
        """Control: a client that flushed unconditionally would re-send stale
        proposals over the VPS's own newer state on every reconnect."""
        monkeypatch.setattr(tls_util, "verify_or_pin", lambda h, f: (True, ""))
        _serving(monkeypatch, _Ws([welcome]))
        flushed: list = []

        async def _flush():
            flushed.append("settings")
        client._flush_pending_settings = _flush
        client._pending_settings = {}

        await client._connect_once()
        await asyncio.sleep(0)

        assert flushed == []
