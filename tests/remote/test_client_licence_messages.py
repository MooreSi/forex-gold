"""What the admin server can make this client do, and what it cannot.

`_connect_loop`'s message loop is the admin channel's business end: a licence
key pushed down it is stored and the app restarts; a revoke deletes the licence
and the token and restarts. Both are irreversible from the client's side, and
until now neither was tested.

The one that matters most is the refusal. `guard.enforce()` sends a client
whose key does not verify back to the activation screen, and that screen
reconnects here — so **saving a key that does not verify restarts the app
straight into the same screen, forever**. An admin console running a retired
signing scheme is exactly the case that produces it. The client has to reject
the key rather than store it.

Reuses the harness in test_client_connect_loop.py: no sockets, no restarts, no
licence written anywhere real.
"""
from __future__ import annotations

import json

import pytest
import websockets

from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote.protocol import (
    MSG_GET_DIAG, MSG_LICENCE, MSG_PING, MSG_PONG, MSG_REVOKE, MSG_WELCOME,
)

from tests.remote.test_client_connect_loop import (  # noqa: F401
    _one_connection, _run, _Ws, env, restarts, store, token_file,
)

# Every script starts with a welcome: `_connect_loop` consumes one frame with
# `recv()` to decide whether it was accepted before it enters the `async for`
# message loop at all. Without it the first scripted message is eaten by the
# handshake and never dispatched — which looks exactly like the handler being
# broken.
WELCOME = {"type": MSG_WELCOME}

GOOD = {
    "type": MSG_LICENCE, "licence_key": "KEY-1", "expiry_date": "2027-01-01",
    "licence_type": "Perpetual", "email": "a@b.c", "machine_id": "M-1",
}


@pytest.fixture
def verifier(monkeypatch):
    """Control whether a pushed key verifies, and record what was checked."""
    seen: list = []

    def _verify(machine_id, expiry, key):
        seen.append((machine_id, expiry, key))
        return seen and _verify.ok
    _verify.ok = True
    monkeypatch.setattr(
        "backend.src.config.licence.verify.verify_licence_key", _verify)
    monkeypatch.setattr(
        "backend.src.config.licence.fingerprint.get_fingerprint", lambda: "FP")
    return _verify, seen


@pytest.fixture
def saved(monkeypatch):
    """Capture licence writes instead of touching the real store."""
    written: list = []
    existing: dict = {}

    class _Store:
        @staticmethod
        def load():
            return dict(existing) or None

        @staticmethod
        def save(payload):
            written.append(payload)
            existing.update(payload)

        @staticmethod
        def clear():
            existing.clear()

    monkeypatch.setattr(rc, "_licence_store", _Store)
    monkeypatch.setattr(rc.asyncio, "sleep", _no_wait)
    return written, existing


async def _no_wait(_s):
    return None


pytestmark = pytest.mark.asyncio


class TestAPushedLicenceThatDoesNotVerify:
    async def test_it_is_not_stored(self, env, restarts, saved, verifier,
                                    monkeypatch):
        """The whole point. Storing it restarts the app into the activation
        screen, which reconnects here, which stores it again."""
        written, _ = saved
        verify, _seen = verifier
        verify.ok = False
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert written == []

    async def test_it_does_not_restart(self, env, restarts, saved, verifier,
                                       monkeypatch):
        verify, _ = verifier
        verify.ok = False
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert restarts == []

    async def test_it_is_checked_against_THIS_machine(self, env, restarts,
                                                      saved, verifier, monkeypatch):
        """A key signed for a different machine must not open this one."""
        _verify, seen = verifier
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert seen and seen[0][0] == "M-1"

    async def test_the_fingerprint_is_used_when_the_push_names_no_machine(
        self, env, restarts, saved, verifier, monkeypatch,
    ):
        _verify, seen = verifier
        _one_connection(monkeypatch, _Ws([WELCOME, dict(GOOD, machine_id="")]))

        await _run()

        assert seen and seen[0][0] == "FP"


class TestAPushedLicenceThatVerifies:
    async def test_it_is_stored(self, env, restarts, saved, verifier, monkeypatch):
        written, _ = saved
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert written and written[0]["licence_key"] == "KEY-1"

    async def test_the_stored_record_carries_the_expiry_and_type(
        self, env, restarts, saved, verifier, monkeypatch,
    ):
        written, _ = saved
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert written[0]["expiry_date"] == "2027-01-01"
        assert written[0]["licence_type"] == "Perpetual"

    async def test_it_restarts(self, env, restarts, saved, verifier, monkeypatch):
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))
        # AFTER _one_connection, which patches sleep to end the loop. The
        # restart happens after a deliberate 3s pause that lets the UI show
        # "Activating...", so a sleep that aborts would stop the very thing
        # under test.
        monkeypatch.setattr(rc.asyncio, "sleep", _no_wait)

        await _run()

        assert restarts == ["restart"]

    async def test_the_ui_is_signalled_before_the_process_dies(
        self, env, restarts, saved, verifier, monkeypatch,
    ):
        """guard.py's timer navigates the browser to a clean "Activating..."
        page. Killing the process without signalling leaves the user looking
        at a dead tab."""
        rc.licence_activated.clear()
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))

        await _run()

        assert rc.licence_activated.is_set()


class TestTheSameLicencePushedAgain:
    async def test_it_does_not_restart_a_second_time(
        self, env, restarts, saved, verifier, monkeypatch,
    ):
        """The server re-pushes on reconnect. Restarting every time would put
        the app in a loop that looks exactly like a crash."""
        _written, existing = saved
        existing.update({"licence_key": "KEY-1"})
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))
        # AFTER _one_connection: with the abort-on-sleep left in place this
        # test passes whether or not the guard exists, because the 3s pause
        # before the restart ends the loop either way. Mutation found that.
        monkeypatch.setattr(rc.asyncio, "sleep", _no_wait)

        await _run()

        assert restarts == []

    async def test_a_DIFFERENT_key_does_restart(
        self, env, restarts, saved, verifier, monkeypatch,
    ):
        """Negative control: a renewal must still take effect."""
        _written, existing = saved
        existing.update({"licence_key": "OLD-KEY"})
        _one_connection(monkeypatch, _Ws([WELCOME, GOOD]))
        monkeypatch.setattr(rc.asyncio, "sleep", _no_wait)

        await _run()

        assert restarts == ["restart"]


class TestAnIncompletePush:
    @pytest.mark.parametrize("bad", [
        {"type": MSG_LICENCE, "expiry_date": "2027-01-01"},
        {"type": MSG_LICENCE, "licence_key": "KEY-1"},
        {"type": MSG_LICENCE},
    ])
    async def test_it_is_ignored(self, env, restarts, saved, verifier,
                                 monkeypatch, bad):
        written, _ = saved
        _one_connection(monkeypatch, _Ws([WELCOME, bad]))

        await _run()

        assert written == [] and restarts == []


class TestRevocation:
    async def test_it_clears_the_licence(self, env, restarts, store, monkeypatch):
        """The restart is not the point — a machine that restarts still
        holding a withdrawn licence comes back up licensed. Mutation found
        that nothing here checked the clear actually happened."""
        _one_connection(monkeypatch, _Ws([WELCOME, {"type": MSG_REVOKE}]))

        await _run()

        assert store == [True]

    async def test_it_clears_the_token_too(self, env, restarts, store,
                                           token_file, monkeypatch):
        """The token is what lets it reconnect and be re-approved silently."""
        _one_connection(monkeypatch, _Ws([WELCOME, {"type": MSG_REVOKE}]))

        await _run()

        assert not token_file.exists()

    async def test_it_restarts(self, env, restarts, store, monkeypatch):
        _one_connection(monkeypatch, _Ws([WELCOME, {"type": MSG_REVOKE}]))

        await _run()

        assert restarts == ["restart"]

    async def test_a_failure_to_clear_still_restarts(
        self, env, restarts, monkeypatch,
    ):
        """Both clears are wrapped for a reason: a revoke that gives up half
        way leaves the machine running on a licence the admin has withdrawn."""
        class _Broken:
            @staticmethod
            def clear():
                raise OSError("read-only filesystem")
        monkeypatch.setattr(rc, "_licence_store", _Broken)
        monkeypatch.setattr(rc.asyncio, "sleep", _no_wait)
        _one_connection(monkeypatch, _Ws([WELCOME, {"type": MSG_REVOKE}]))

        await _run()

        assert restarts == ["restart"]


class TestTheOrdinaryTraffic:
    async def test_a_ping_is_ponged(self, env, restarts, monkeypatch):
        ws = _Ws([WELCOME, {"type": MSG_PING}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert MSG_PONG in ws.types()

    async def test_a_diagnostics_request_is_answered(self, env, restarts,
                                                     monkeypatch):
        monkeypatch.setattr(rc, "_build_diagnostics", lambda: {"type": "diag"})
        ws = _Ws([WELCOME, {"type": MSG_GET_DIAG}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert "diag" in ws.types()

    async def test_a_malformed_frame_does_not_drop_the_link(
        self, env, restarts, monkeypatch,
    ):
        """One bad frame must cost that frame. Raising here ends the receive
        loop and reconnects, and a server that keeps sending it would hold the
        client in a reconnect cycle."""
        ws = _Ws([WELCOME, "{not json", {"type": MSG_PING}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert MSG_PONG in ws.types()

    async def test_an_unknown_type_is_ignored(self, env, restarts, monkeypatch):
        """The admin server may be newer than this client."""
        ws = _Ws([WELCOME, {"type": "MSG_FROM_A_LATER_VERSION"}, {"type": MSG_PING}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert MSG_PONG in ws.types()
