"""The VPS's front door, and the switch that stops it trading.

Two things here decide something the rest of the app cannot second-guess:

  * _check_token()/_handle_connection() are the ONLY thing between the open
    internet and a websocket that can place market orders. The TLS layer does
    not authenticate the peer at all (see docs/todo/bugs/014), so this token
    is the whole of it.
  * is_standing_down() is described in its own docstring as "the single gate
    for every trade-opening path". STAND_DOWN hands control to the Mac; if the
    gate does not follow, both nodes trade the same account at once.

Nothing here opens a socket. The websocket is a fake that records frames and
can be told to misbehave during the handshake.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync.server import SyncServer
from backend.src.services.cluster.sync.protocol import (
    MSG_HELLO, MSG_REJECT, MSG_WELCOME, TRADER_LOCAL, TRADER_REMOTE_VPS,
)


pytestmark = pytest.mark.usefixtures("fresh_db")


class _Ws:
    """A fake peer. `frames` is what it will deliver; `sent` is what it got."""

    def __init__(self, frames=(), recv_error=None):
        self._frames = list(frames)
        self._recv_error = recv_error
        self.sent: list[dict] = []
        self.closed = False
        self.remote_address = ("203.0.113.9", 51000)

    async def recv(self):
        if self._recv_error is not None:
            raise self._recv_error
        if not self._frames:
            raise ConnectionError("peer went away")
        return self._frames.pop(0)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        async def _gen():
            while self._frames:
                yield self._frames.pop(0)
        return _gen()

    def types(self):
        return [f.get("type") for f in self.sent]


@pytest.fixture
def server():
    s = SyncServer()
    s._token = "a-high-entropy-shared-secret"
    return s


def _hello(token):
    return json.dumps({"type": MSG_HELLO, "token": token})


class TestTheTokenComparison:
    def test_the_right_token_passes(self, server):
        assert server._check_token("a-high-entropy-shared-secret") is True

    def test_a_wrong_token_fails(self, server):
        assert server._check_token("not-the-secret") is False

    def test_an_empty_token_fails(self, server):
        assert server._check_token("") is False

    def test_A_SERVER_WITH_NO_TOKEN_ACCEPTS_NOBODY(self, server):
        """The dangerous default. An unconfigured server must reject
        everything, including an empty token -- not match empty against empty
        and let the caller in."""
        server._token = None
        assert server._check_token("") is False
        assert server._check_token("anything") is False

    def test_a_prefix_of_the_token_fails(self, server):
        assert server._check_token("a-high-entropy") is False

    def test_it_is_a_constant_time_comparison(self):
        """compare_digest, not ==. The token is the only authentication on
        this channel, and the TLS layer does not verify the peer at all."""
        import inspect
        src = inspect.getsource(SyncServer._check_token)
        assert "compare_digest" in src


@pytest.mark.asyncio
class TestTheHandshake:
    async def test_a_good_hello_is_welcomed(self, server):
        ws = _Ws([_hello("a-high-entropy-shared-secret")])

        await server._handle_connection(ws)

        assert MSG_WELCOME in ws.types()

    async def test_a_bad_token_is_rejected_and_closed(self, server):
        ws = _Ws([_hello("wrong")])

        await server._handle_connection(ws)

        assert ws.types() == [MSG_REJECT]
        assert ws.closed is True

    async def test_A_REJECTED_PEER_IS_NEVER_A_CLIENT(self, server):
        """What actually matters. _clients is the broadcast set -- a rejected
        peer left in it receives settings, status and ledger pushes."""
        ws = _Ws([_hello("wrong")])

        await server._handle_connection(ws)

        assert ws not in server._clients
        assert server._clients == set()

    async def test_the_first_message_MUST_be_hello(self, server):
        """Even carrying the right token. Otherwise a peer can skip the
        handshake by opening with any other message type."""
        ws = _Ws([json.dumps({"type": "settings_propose",
                              "token": "a-high-entropy-shared-secret",
                              "updates": {"risk_percent": 99}})])

        await server._handle_connection(ws)

        assert ws.types() == [MSG_REJECT]
        assert server._clients == set()

    async def test_a_hello_with_no_token_field_is_rejected(self, server):
        ws = _Ws([json.dumps({"type": MSG_HELLO})])

        await server._handle_connection(ws)

        assert ws.types() == [MSG_REJECT]

    async def test_malformed_json_closes_without_a_reply(self, server):
        """A handshake that never parsed gets no reply frame -- there is
        nothing to reply to, and the peer is not a client."""
        ws = _Ws(["{not json"])

        await server._handle_connection(ws)

        assert ws.sent == []
        assert ws.closed is True
        assert server._clients == set()

    async def test_a_peer_that_says_nothing_is_dropped(self, server):
        """The handshake read is wrapped in wait_for(timeout=10). A peer that
        connects and stalls must not hold a slot open."""
        ws = _Ws(recv_error=asyncio.TimeoutError())

        await server._handle_connection(ws)

        assert server._clients == set()
        assert ws.closed is True

    async def test_an_accepted_client_is_REMOVED_when_it_disconnects(self, server):
        """Otherwise every broadcast writes to a dead socket for the life of
        the process."""
        ws = _Ws([_hello("a-high-entropy-shared-secret")])

        await server._handle_connection(ws)

        assert server._clients == set()

    async def test_the_socket_is_ALWAYS_closed_on_the_way_out(self, server):
        """The async-for exits on the peer's FIN without completing our own
        side of the close handshake. Without the explicit close these sit in
        CLOSE_WAIT forever -- 75+ were found stuck on the VPS, eventually
        making new connections time out."""
        ws = _Ws([_hello("a-high-entropy-shared-secret")])

        await server._handle_connection(ws)

        assert ws.closed is True

    async def test_the_welcome_carries_the_state_the_mac_needs(self, server):
        ws = _Ws([_hello("a-high-entropy-shared-secret")])

        await server._handle_connection(ws)

        welcome = ws.sent[0]
        for key in ("settings", "channel_strategy", "trading_schedule",
                    "strategy_params", "active_trader", "node_id"):
            assert key in welcome, f"WELCOME is missing {key}"


class _Engine:
    def __init__(self, running=True):
        self.is_running = running
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.is_running = True

    def stop(self):
        self.stopped = True
        self.is_running = False


@pytest.mark.asyncio
class TestStandDown:
    """Hands trading control to the Mac. Both nodes trading the same account
    is the failure being prevented."""

    async def test_it_hands_the_active_trader_to_the_mac(self, server):
        from backend.src.db import database as db_module
        await server._handle_stand_down(_Ws())
        assert db_module.get_active_trader() == TRADER_LOCAL

    async def test_the_TRADE_GATE_FOLLOWS(self, server):
        """is_standing_down() is the single gate every trade-opening path
        checks. If it did not follow the switch, the VPS would keep opening
        trades after handing control away."""
        assert server.is_standing_down() is False
        await server._handle_stand_down(_Ws())
        assert server.is_standing_down() is True

    async def test_it_stops_the_running_engines(self, server):
        server._breakout_engine = _Engine(running=True)
        server._re_engine = _Engine(running=True)

        await server._handle_stand_down(_Ws())

        assert server._breakout_engine.stopped is True
        assert server._re_engine.stopped is True

    async def test_it_RECORDS_WHICH_engines_it_stopped(self, server):
        """So RESUME restarts only what sync itself paused. An engine the
        user had already stopped must not come back on."""
        from backend.src.db import database as db_module
        server._breakout_engine = _Engine(running=True)
        server._re_engine = _Engine(running=False)      # user had it off

        await server._handle_stand_down(_Ws())

        stopped = db_module.get_stood_down_engines()
        assert "breakout" in stopped
        assert "re" not in stopped and "reversal" not in stopped

    async def test_the_ack_reports_the_open_positions(self, server):
        """The Mac shows these to the user before taking over -- it is what
        the handover screen is built from."""
        class _Main:
            def get_open_trades(self):
                return [{"trade_id": "T1", "direction": "BUY",
                         "entry_price": 4500.0, "strategy": "scalp"}]
        server._main_engine = _Main()
        ws = _Ws()

        await server._handle_stand_down(ws)

        assert ws.sent[0]["open_positions"] == [
            {"trade_id": "T1", "direction": "BUY",
             "entry_price": 4500.0, "strategy": "scalp"}]

    async def test_a_failing_position_lookup_still_acks(self, server):
        """No ack means the Mac waits out its 15s timeout and does not know
        whether the VPS stood down. Standing down with an unknown position
        list is better than an unanswered handover."""
        class _Main:
            def get_open_trades(self):
                raise RuntimeError("bridge down")
        server._main_engine = _Main()
        ws = _Ws()

        await server._handle_stand_down(ws)

        assert ws.sent[0]["open_positions"] == []


@pytest.mark.asyncio
class TestResume:
    async def test_it_takes_trading_back(self, server):
        from backend.src.db import database as db_module
        await server._handle_stand_down(_Ws())

        await server._handle_resume(_Ws())

        assert db_module.get_active_trader() == TRADER_REMOTE_VPS
        assert server.is_standing_down() is False

    async def test_it_restarts_ONLY_what_stand_down_stopped(self, server):
        server._breakout_engine = _Engine(running=True)
        server._re_engine = _Engine(running=False)      # user had it off
        await server._handle_stand_down(_Ws())

        await server._handle_resume(_Ws())

        assert server._breakout_engine.started is True
        assert server._re_engine.started is False

    async def test_the_stopped_list_is_cleared_afterwards(self, server):
        """Otherwise a later RESUME restarts engines the user has since
        stopped by hand."""
        from backend.src.db import database as db_module
        server._breakout_engine = _Engine(running=True)
        await server._handle_stand_down(_Ws())

        await server._handle_resume(_Ws())

        assert db_module.get_stood_down_engines() == []

    async def test_it_acks_with_what_it_restarted(self, server):
        server._breakout_engine = _Engine(running=True)
        await server._handle_stand_down(_Ws())
        ws = _Ws()

        await server._handle_resume(ws)

        assert ws.sent[0]["restarted_engines"] == ["breakout"]
