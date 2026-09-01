"""What the client does with what the admin server tells it.

`_connect_loop` is the client half of the remote-management channel: it dials
the admin server, identifies itself, and then acts on whatever arrives. Two of
those actions are irreversible from the client's side, and both were untested:

  * **`reject: revoked`** deletes the licence key AND the websocket token, then
    hard-exits so the app comes back on the registration screen. This is the
    kill switch. If it half-works — token deleted, licence kept, or the other
    way round — the app comes back up in a state nobody designed.
  * **`reject: invalid_token`** sends a registration request, and records
    `registration_sent_at` **only if the send actually succeeded**. That
    distinction is not cosmetic: on 2026-08-07 the activation screen told a
    user their request was awaiting approval while the admin server was down
    and nothing had left the machine.

Plus the ordinary path: a welcome populates the status the dashboard renders,
persists the admin flag, and enables auto-start for future launches.

No sockets and no restarts. `websockets.connect` is replaced via
`monkeypatch.setattr` on the module attribute (never by swapping
`sys.modules`), and `_do_restart` is recorded rather than performed.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote.protocol import (
    MSG_REJECT, MSG_WELCOME,
)

pytestmark = pytest.mark.asyncio


class _Stop(BaseException):
    """Ends the `while True` after the scripted connections are used up.

    Derived from BaseException on purpose: `_connect_loop`'s own
    `except Exception` would otherwise swallow it, log it as a connection
    error, and overwrite `_status["last_error"]` -- hiding exactly the value
    several of these tests are checking.
    """


class _Ws:
    def __init__(self, replies, send_fails=False):
        self._replies = list(replies)
        self.sent: list = []
        self.send_fails = send_fails

    async def send(self, raw):
        if self.send_fails:
            raise ConnectionResetError("the socket went away mid-send")
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._replies:
            raise asyncio.TimeoutError("nothing more")
        r = self._replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._replies:
            raise StopAsyncIteration
        r = self._replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def restarts(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "_do_restart", lambda: calls.append("restart"))
    return calls


@pytest.fixture
def store(monkeypatch):
    """Records whether the licence store was cleared."""
    cleared: list = []

    class _Store:
        @staticmethod
        def clear():
            cleared.append(True)
    monkeypatch.setattr(rc, "_licence_store", _Store)
    return cleared


@pytest.fixture
def token_file(monkeypatch, tmp_path):
    f = tmp_path / "ws_token"
    f.write_text("a-token", encoding="utf-8")
    monkeypatch.setattr(rc, "_TOKEN_FILE", f)
    monkeypatch.setattr(rc, "get_or_create_token", lambda: "a-token")
    return f


@pytest.fixture
def env(monkeypatch, tmp_path, token_file):
    """Everything the loop touches, pointed away from the real install."""
    # Takes the host, as the real one does since bugs/014 stage 2: the context
    # differs per path (CA-verified on the internet, TOFU on the LAN), so a
    # zero-argument fake would be testing a signature that no longer exists.
    monkeypatch.setattr(rc, "client_ssl_context", lambda host=None: None)
    # The loop establishes the peer before sending the hello. These tests are
    # about what happens AFTER a connection is accepted, so the check is made
    # to pass; its own behaviour is covered in
    # tests/remote/test_remote_client_verification.py.
    monkeypatch.setattr(rc, "peer_is_acceptable", lambda host, presented: (True, "test"))
    monkeypatch.setattr(rc, "_REMOTE_DIR", tmp_path)

    async def _no_lan(timeout=6.0):
        return None
    monkeypatch.setattr(rc, "_discover_lan_server", _no_lan)
    monkeypatch.setattr(rc, "_build_hello", lambda: {"type": "hello",
                                                     "token": "a-token"})
    monkeypatch.setattr(rc, "_build_register", lambda: {"type": "register",
                                                        "token": "a-token"})

    saved: dict = {}

    class _Cfg:
        @staticmethod
        def get(key, default=None):
            return saved.get(key, default)

        @staticmethod
        def save_to_yaml(fields):
            saved.update(fields)
    monkeypatch.setattr(rc, "_app_config", _Cfg)

    for k in list(rc._status):
        if isinstance(rc._status[k], bool):
            rc._status[k] = False
    rc._status["last_error"] = ""
    rc._status.pop("registration_sent_at", None)
    return saved


def _one_connection(monkeypatch, ws):
    """Serve `ws` once, then break out of the reconnect loop."""
    state = {"n": 0}

    def _connect(*_a, **_kw):
        state["n"] += 1
        if state["n"] > 1:
            raise _Stop
        return ws
    monkeypatch.setattr(websockets, "connect", _connect)

    async def _sleep(_s):
        raise _Stop
    monkeypatch.setattr(rc.asyncio, "sleep", _sleep)


async def _run():
    try:
        await rc._connect_loop()
    except _Stop:
        pass


class TestRevocationIsAKillSwitch:
    """Both files, then restart. Half of it is a state nobody designed."""

    async def test_it_clears_the_licence_AND_the_token(self, env, store,
                                                       token_file, restarts,
                                                       monkeypatch):
        _one_connection(monkeypatch, _Ws([{"type": MSG_REJECT,
                                           "reason": "revoked"}]))

        await _run()

        assert store == [True], "the licence key survived revocation"
        assert not token_file.exists(), "the websocket token survived revocation"
        assert restarts == ["restart"]

    async def test_an_ORDINARY_rejection_clears_neither(self, env, store,
                                                        token_file, restarts,
                                                        monkeypatch):
        """Control. A rejection for any other reason — a server hiccup, an
        unknown token — must not wipe a paid licence off the machine."""
        _one_connection(monkeypatch, _Ws([{"type": MSG_REJECT,
                                           "reason": "server busy"}]))

        await _run()

        assert store == []
        assert token_file.exists()
        assert restarts == []
        assert "server busy" in rc._status["last_error"]


class TestTheRegistrationRequestIsOnlyClaimedWhenItWasSent:
    """2026-08-07: the activation screen told a user their request was awaiting
    approval while the admin server was down and nothing had been transmitted.
    `registration_sent_at` is the positive proof, and it must only be stamped
    when the send actually succeeded."""

    async def test_a_successful_send_is_recorded(self, env, restarts,
                                                 monkeypatch):
        ws = _Ws([{"type": MSG_REJECT, "reason": "invalid_token"}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert ws.types() == ["hello", "register"]
        assert rc._status.get("registration_sent_at")
        assert rc._status["last_error"] == "awaiting admin approval"

    async def test_a_FAILED_send_is_NOT_recorded(self, env, restarts,
                                                 monkeypatch):
        """The socket dies between the rejection and the registration."""
        class _DiesOnRegister(_Ws):
            async def send(self, raw):
                m = json.loads(raw)
                if m.get("type") == "register":
                    raise ConnectionResetError("socket closed")
                self.sent.append(m)

        ws = _DiesOnRegister([{"type": MSG_REJECT, "reason": "invalid_token"}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert rc._status.get("registration_sent_at") in (None, 0), (
            "the app claimed a registration was sent when it never left the "
            "machine -- this is the 2026-08-07 bug"
        )

    async def test_the_client_is_not_marked_connected_either_way(self, env,
                                                                 restarts,
                                                                 monkeypatch):
        _one_connection(monkeypatch, _Ws([{"type": MSG_REJECT,
                                           "reason": "invalid_token"}]))

        await _run()

        assert rc._status["connected"] is False


class TestAWelcome:

    async def test_it_populates_what_the_dashboard_shows(self, env, restarts,
                                                         monkeypatch):
        _one_connection(monkeypatch, _Ws([{
            "type": MSG_WELCOME,
            "subscription_type": "Annual",
            "expiry_date": "2027-01-01",
            "email": "simon@example.com",
            "is_remote_admin": False,
        }]))

        await _run()

        assert rc._status["subscription_type"] == "Annual"
        assert rc._status["subscription_expiry"] == "2027-01-01"
        assert rc._status["email"] == "simon@example.com"
        assert rc._status["last_error"] == ""

    async def test_it_enables_auto_start_for_future_launches(self, env,
                                                             restarts,
                                                             monkeypatch):
        """Without this the loop only ever runs when someone presses the
        Request Registration button by hand."""
        _one_connection(monkeypatch, _Ws([{"type": MSG_WELCOME}]))

        await _run()

        assert env.get("remote_admin_client_enabled") is True

    async def test_the_admin_flag_is_persisted_so_the_button_survives_a_reload(
            self, env, restarts, monkeypatch, tmp_path):
        _one_connection(monkeypatch, _Ws([{"type": MSG_WELCOME,
                                           "is_remote_admin": True}]))

        await _run()

        assert rc._status["is_remote_admin"] is True
        assert (tmp_path / "is_remote_admin").exists()

    async def test_the_flag_is_REMOVED_when_admin_rights_are_withdrawn(
            self, env, restarts, monkeypatch, tmp_path):
        """Control for the test above, and the direction that matters more:
        a stale flag file leaves the admin button on a machine that is no
        longer authorised."""
        (tmp_path / "is_remote_admin").touch()
        _one_connection(monkeypatch, _Ws([{"type": MSG_WELCOME,
                                           "is_remote_admin": False}]))

        await _run()

        assert not (tmp_path / "is_remote_admin").exists()


class TestAnUnexpectedFirstReply:
    async def test_anything_that_is_not_a_welcome_leaves_it_disconnected(
            self, env, restarts, monkeypatch):
        _one_connection(monkeypatch, _Ws([{"type": "something_else"}]))

        await _run()

        assert rc._status["connected"] is False
