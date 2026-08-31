"""Who gets to send this node a trade order.

`SyncServer._dispatch` routes `MSG_SIGNAL_ORDER` and `MSG_MARKET_ORDER` — and
the server's own comment says of the first: *"Calls open_trade() directly"*. So
the handshake in `_handle_connection` is not an availability concern. **It is
the only thing between a socket and a real order on a real account.**

The properties pinned here are the ones whose absence a green run cannot show:

  * A socket that fails the handshake must never reach `_dispatch`, and must
    never land in `_clients` (which is what `_broadcast` iterates).
  * The token check is constant-time, and an empty token on either side fails.
  * A good token in the wrong message type is still refused — being able to
    send *a* valid token is not the same as completing the handshake.
  * Every connection is discarded AND explicitly closed on the way out. The
    async-for exits on the peer's FIN without finishing our side of the close;
    75+ sockets were found stuck in CLOSE_WAIT on the VPS from this, eventually
    making new connections time out.

No sockets and no server: the websocket is a fake serving scripted frames, and
`_dispatch` is replaced by a spy so that "did an unauthenticated peer reach the
order path" is a direct assertion rather than an inference.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import (
    MSG_HELLO, MSG_MODEL_SNAPSHOT_END, MSG_MODEL_SNAPSHOT_UPLOAD, MSG_PING,
    MSG_PONG, MSG_REJECT, MSG_SIGNAL_ORDER, MSG_WELCOME,
)

pytestmark = pytest.mark.asyncio

TOKEN = "a-shared-secret-token"


class _Ws:
    def __init__(self, frames, ip="203.0.113.9"):
        self._frames = list(frames)
        self.remote_address = (ip, 55555)
        self.sent: list = []
        self.closed = False

    async def recv(self):
        if not self._frames:
            raise asyncio.TimeoutError("peer went quiet")
        f = self._frames.pop(0)
        return f if isinstance(f, (str, bytes)) else json.dumps(f)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        f = self._frames.pop(0)
        return f if isinstance(f, (str, bytes)) else json.dumps(f)

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def server(monkeypatch):
    """A SyncServer with its token set and its database reads stubbed."""
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._token = TOKEN
    srv._clients = set()
    srv._last_seen_ts = 0.0
    srv._liveness_alerted = False
    srv._main_engine = None

    for name, value in (
        ("_settings_snapshot", {}),
        ("_channel_strategy_snapshot", {}),
        ("_trading_schedule_snapshot", {}),
        ("_strategy_params_snapshot", {}),
    ):
        monkeypatch.setattr(ss.SyncServer, name, lambda _s, _v=value: _v)
    monkeypatch.setattr(ss.db_module, "get_active_trader", lambda: "mac")
    monkeypatch.setattr(ss.db_module, "get_or_create_node_id", lambda: "node-1")
    return srv


@pytest.fixture
def dispatched(monkeypatch):
    """Every message that reached the router -- i.e. the order path."""
    seen: list = []

    async def _spy(_self, _ws, msg):
        seen.append(msg.get("type"))
    monkeypatch.setattr(ss.SyncServer, "_dispatch", _spy)
    return seen


def _hello(token=TOKEN):
    return {"type": MSG_HELLO, "token": token}


class TestTheHandshakeIsTheOnlyThingBetweenASocketAndAnOrder:

    async def test_a_bad_token_never_reaches_the_router(self, server, dispatched):
        ws = _Ws([_hello("wrong-token"),
                  {"type": MSG_SIGNAL_ORDER, "direction": "BUY"}])

        await server._handle_connection(ws)

        assert dispatched == [], "an unauthenticated peer reached the order path"
        assert ws.types() == [MSG_REJECT]
        assert ws.closed is True
        assert server._clients == set()

    async def test_an_empty_token_from_the_peer_is_refused(self, server,
                                                           dispatched):
        ws = _Ws([_hello("")])

        await server._handle_connection(ws)

        assert dispatched == []
        assert ws.types() == [MSG_REJECT]

    async def test_no_token_configured_on_THIS_side_refuses_everyone(
            self, server, dispatched):
        """An unconfigured server must not accept all comers. Failing open here
        would make an unpaired node accept orders from anyone who found it."""
        server._token = ""
        ws = _Ws([_hello("")])

        await server._handle_connection(ws)

        assert dispatched == []
        assert ws.types() == [MSG_REJECT]

    async def test_no_token_configured_refuses_even_a_plausible_one(
            self, server, dispatched):
        server._token = ""
        ws = _Ws([_hello("anything-at-all")])

        await server._handle_connection(ws)

        assert dispatched == []

    async def test_a_valid_token_in_the_WRONG_message_type_is_refused(
            self, server, dispatched):
        """Holding a valid token is not the same as completing the handshake --
        the first frame must be a HELLO."""
        ws = _Ws([{"type": MSG_PING, "token": TOKEN},
                  {"type": MSG_SIGNAL_ORDER}])

        await server._handle_connection(ws)

        assert dispatched == []
        assert ws.types() == [MSG_REJECT]

    async def test_a_silent_peer_is_dropped_without_being_admitted(
            self, server, dispatched):
        ws = _Ws([])

        await server._handle_connection(ws)

        assert dispatched == []
        assert server._clients == set()
        assert ws.closed is True

    async def test_malformed_json_in_the_handshake_is_dropped(self, server,
                                                              dispatched):
        ws = _Ws(["{not json"])

        await server._handle_connection(ws)

        assert dispatched == []
        assert ws.closed is True

    async def test_the_token_comparison_is_constant_time(self, server,
                                                         monkeypatch):
        """A byte-by-byte `==` on a shared secret leaks its length and prefix
        to anyone who can time the response."""
        import secrets

        calls: list = []
        real = secrets.compare_digest
        monkeypatch.setattr(secrets, "compare_digest",
                            lambda a, b: calls.append((a, b)) or real(a, b))

        assert server._check_token(TOKEN) is True
        assert calls, "_check_token did not use secrets.compare_digest"


class TestAGoodHandshakeIsAdmitted:
    """Positive controls. Without them a server that refused every connection
    would satisfy everything above."""

    async def test_it_gets_a_welcome(self, server, dispatched):
        ws = _Ws([_hello()])

        await server._handle_connection(ws)

        assert ws.types() == [MSG_WELCOME]

    async def test_the_welcome_carries_the_state_the_mac_mirrors(self, server,
                                                                 dispatched):
        ws = _Ws([_hello()])

        await server._handle_connection(ws)

        welcome = ws.sent[0]
        for field in ("settings", "channel_strategy", "trading_schedule",
                      "strategy_params", "active_trader", "node_id"):
            assert field in welcome, f"the Mac mirrors {field} and it is missing"

    async def test_messages_after_it_DO_reach_the_router(self, server,
                                                         dispatched):
        ws = _Ws([_hello(), {"type": MSG_PING}, {"type": MSG_SIGNAL_ORDER}])

        await server._handle_connection(ws)

        assert dispatched == [MSG_PING, MSG_SIGNAL_ORDER]

    async def test_the_connection_is_registered_for_broadcasts(self, server,
                                                               monkeypatch):
        seen: list = []

        async def _spy(_self, _ws, msg):
            seen.append(len(_self._clients))
        monkeypatch.setattr(ss.SyncServer, "_dispatch", _spy)

        ws = _Ws([_hello(), {"type": MSG_PING}])
        await server._handle_connection(ws)

        assert seen == [1], "the peer was never added to _clients"

    async def test_a_successful_handshake_stamps_last_seen(self, server,
                                                           dispatched):
        """The liveness watchdog reads this. A handshake that does not stamp it
        leaves the watchdog alerting on a node that just connected."""
        ws = _Ws([_hello()])

        await server._handle_connection(ws)

        assert server._last_seen_ts > 0

    async def test_connecting_clears_a_standing_liveness_alert(self, server,
                                                               dispatched):
        server._liveness_alerted = True
        ws = _Ws([_hello()])

        await server._handle_connection(ws)

        assert server._liveness_alerted is False


class TestTheSocketIsAlwaysCleanedUp:
    """75+ sockets were found stuck in CLOSE_WAIT on the VPS, eventually making
    new connection attempts time out. The `finally` block is the fix and it has
    to cover every exit."""

    async def test_a_normal_disconnect_discards_and_closes(self, server,
                                                           dispatched):
        ws = _Ws([_hello(), {"type": MSG_PING}])

        await server._handle_connection(ws)

        assert server._clients == set()
        assert ws.closed is True

    async def test_a_handler_that_raises_still_discards_and_closes(
            self, server, monkeypatch):
        async def _boom(_self, _ws, _msg):
            raise RuntimeError("handler blew up")
        monkeypatch.setattr(ss.SyncServer, "_dispatch", _boom)

        ws = _Ws([_hello(), {"type": MSG_PING}])

        await server._handle_connection(ws)   # must not propagate

        assert server._clients == set()
        assert ws.closed is True

    async def test_a_close_that_itself_fails_does_not_propagate(self, server,
                                                                dispatched):
        class _BadClose(_Ws):
            async def close(self):
                raise OSError("socket already gone")

        ws = _BadClose([_hello()])

        await server._handle_connection(ws)   # must not raise

        assert server._clients == set()


class TestBinaryFramesOnlyCountDuringAnUpload:
    """Bytes arriving outside an upload window must be dropped, not buffered.

    Observing "not dispatched" is not enough -- that is true either way. The
    property is that they cannot reach `unpack_models`, which writes model
    files to disk. Confirmed by mutation: buffering them unconditionally left
    the weaker version of this test green.
    """

    @pytest.fixture
    def unpacked(self, monkeypatch):
        got: list = []
        import backend.src.services.cluster.sync.model_transfer as mt
        monkeypatch.setattr(mt, "unpack_models", lambda blob, d: got.append(blob) or [])
        monkeypatch.setattr(mt, "data_dir", lambda: "/tmp/nowhere")
        return got

    async def test_a_stray_binary_frame_never_reaches_the_unpacker(
            self, server, dispatched, unpacked):
        ws = _Ws([
            _hello(),
            b"\xde\xad\xbe\xef",                    # outside any upload
            {"type": MSG_MODEL_SNAPSHOT_END},
        ])

        await server._handle_connection(ws)

        assert unpacked == [b""], (
            f"bytes sent outside an upload window were fed to the model "
            f"unpacker: {unpacked}"
        )

    async def test_a_stray_binary_frame_is_not_dispatched_either(
            self, server, dispatched):
        ws = _Ws([_hello(), b"\x00" * 16, {"type": MSG_PING}])

        await server._handle_connection(ws)

        assert dispatched == [MSG_PING]

    async def test_bytes_inside_an_upload_DO_reach_the_unpacker(
            self, server, dispatched, unpacked):
        """Control: the upload path still works, or the test above would pass
        on a server that dropped every byte."""
        ws = _Ws([
            _hello(),
            {"type": MSG_MODEL_SNAPSHOT_UPLOAD},
            b"model-bytes",
            {"type": MSG_MODEL_SNAPSHOT_END},
        ])

        await server._handle_connection(ws)

        assert unpacked == [b"model-bytes"]

    async def test_the_buffer_is_reset_between_uploads(self, server,
                                                       dispatched, unpacked):
        ws = _Ws([
            _hello(),
            {"type": MSG_MODEL_SNAPSHOT_UPLOAD},
            b"first",
            {"type": MSG_MODEL_SNAPSHOT_END},
            {"type": MSG_MODEL_SNAPSHOT_UPLOAD},
            b"second",
            {"type": MSG_MODEL_SNAPSHOT_END},
        ])

        await server._handle_connection(ws)

        assert unpacked == [b"first", b"second"], "the buffer leaked between uploads"

    async def test_an_ABANDONED_upload_does_not_corrupt_the_retry(
            self, server, dispatched, unpacked):
        """The case the reset at the START of an upload exists for: the Mac
        begins a snapshot, something goes wrong before the END, and it starts
        again on the same connection. Concatenating the two would hand
        `unpack_models` a corrupt archive.

        The reset at the END covers the ordinary sequence, so only this shape
        distinguishes them -- confirmed by mutation, which the previous test
        alone did not catch.
        """
        ws = _Ws([
            _hello(),
            {"type": MSG_MODEL_SNAPSHOT_UPLOAD},
            b"half-a-model",                        # abandoned, no END
            {"type": MSG_MODEL_SNAPSHOT_UPLOAD},    # the Mac starts over
            b"the-real-model",
            {"type": MSG_MODEL_SNAPSHOT_END},
        ])

        await server._handle_connection(ws)

        assert unpacked == [b"the-real-model"], (
            f"the abandoned upload was concatenated onto the retry: {unpacked}"
        )


class TestDispatchRoutingIsExhaustive:
    async def test_an_unknown_type_does_nothing_at_all(self, server):
        """No exception, no side effect, no reply. A router that fell through
        to a default handler would be a very bad place to be wrong."""
        ws = _Ws([])

        await server._dispatch(ws, {"type": "not_a_real_message"})

        assert ws.sent == []

    async def test_a_ping_is_answered(self, server):
        """Control: dispatch does route the types it knows."""
        ws = _Ws([])

        await server._dispatch(ws, {"type": MSG_PING})

        assert ws.types() == [MSG_PONG]
