"""Who gets in, over the remote server's one front door.

`_handler` is the entry point for every remote connection -- every client and
every admin machine. It decides, before anything else runs, whether a socket
is an authorised admin, a known client, a stranger asking to register, or
something to drop on the floor. At 304 lines it was the largest untested block
in the file (145 of its lines uncovered), while `test_admin_auth.py` covers the
password primitive and `test_admin_commands.py` covers what an admin can do
once already inside.

The properties worth pinning here are the ones whose absence a green run cannot
show:

  * A refusal must REFUSE. Every reject branch has to return without reaching
    `_admin_handler` or the welcome, and it is one missing `return` away from
    not doing that.
  * A revoked token must get its revoke notice even when rate-limited --
    otherwise a client that reconnects on a dead token is stuck retrying
    forever and never learns why.
  * An unknown token must NOT be recorded as a rate-limit failure, or ordinary
    new installs would lock themselves out of registering.
  * A registration must never grant anything. It queues, and that is all.
  * The in-flight approval race: a MSG_REGISTER built before the client knew it
    had been approved must not push an already-approved token back into
    pending, which would un-approve it in the admin UI while a valid licence
    already existed.

No socket, no TLS, no threads. The websocket is a fake that serves scripted
frames and records what was sent back.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.remote import auth
from backend.src.services.cluster.remote import server as rs
from backend.src.services.cluster.remote.protocol import (
    MSG_ADMIN_HELLO, MSG_ADMIN_WELCOME, MSG_HELLO, MSG_REGISTER, MSG_REJECT,
    MSG_WELCOME,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    """Serves scripted frames; records what the server sent and whether it was
    closed. Running out of frames means "the client went quiet"."""

    def __init__(self, frames, ip="203.0.113.9"):
        # An exhausted queue raises TimeoutError from recv(), which is the same
        # thing `asyncio.wait_for` raises into `_handler` when a frame never
        # comes -- so the timeout branches are exercised on the identical path,
        # without the suite actually waiting 5 or 20 seconds for each. The
        # bounds themselves are asserted separately, in
        # TestTheTimeoutsAreActuallyBounded.
        self._frames = list(frames)
        self.remote_address = (ip, 51234)
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self):
        if not self._frames:
            raise asyncio.TimeoutError("no more frames")
        frame = self._frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]

    def reasons(self) -> list[str]:
        return [m.get("reason") for m in self.sent if m.get("type") == MSG_REJECT]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Every module global `_handler` touches, redirected away from real state."""
    d = tmp_path / "remote"
    d.mkdir()
    monkeypatch.setattr(rs, "_REMOTE_DIR", d)
    monkeypatch.setattr(rs, "_TOKENS_FILE", d / "allowed_tokens.json")
    monkeypatch.setattr(rs, "_PENDING_FILE", d / "pending_registrations.json")
    monkeypatch.setattr(rs, "_REVOKED_FILE", d / "revoked_tokens.json")
    monkeypatch.setattr(rs, "_ADMIN_MACHINES_FILE", d / "admin_machines.json")
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_revoked_tokens", set())
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_admin_machines", [])
    monkeypatch.setattr(rs, "_auth_failures", {})
    return d


@pytest.fixture
def no_side_effects(monkeypatch):
    """The notify/push fan-out is another module's job; here it would only make
    the tests depend on Telegram and on other sockets."""
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(rs, "_push_pending_to_all_admins", _noop)
    monkeypatch.setattr(rs, "_notify_new_registration", _noop)


@pytest.fixture
def admin_handler_calls(monkeypatch):
    """Records every entry into the privileged channel. An empty list is the
    assertion that matters for each refusal below."""
    calls: list = []

    async def _spy(ws, uuid):
        calls.append(uuid)
    monkeypatch.setattr(rs, "_admin_handler", _spy)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# Before anything is trusted
# ─────────────────────────────────────────────────────────────────────────────

class TestTheDoorClosesOnSilenceAndNoise:

    async def test_a_socket_that_never_speaks_is_dropped(self):
        """The auth timeout. A connection that sends no HELLO must not sit
        there holding a slot."""
        ws = _Ws([])

        await rs._handler(ws)

        assert ws.sent == [], "a silent socket was answered"

    async def test_malformed_json_is_dropped(self):
        ws = _Ws(["{not json at all"])

        await rs._handler(ws)

        assert ws.sent == []

    async def test_a_message_of_an_unknown_type_is_dropped(self):
        ws = _Ws([{"type": "something_else", "token": "T"}])

        await rs._handler(ws)

        assert ws.sent == []


# ─────────────────────────────────────────────────────────────────────────────
# The privileged admin channel
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminHelloRefusals:
    """Each of these is one missing `return` away from admitting the caller."""

    async def test_no_machine_uuid_is_refused(self, admin_handler_calls):
        ws = _Ws([{"type": MSG_ADMIN_HELLO, "password": "hunter2"}])

        await rs._handler(ws)

        assert ws.reasons() == ["no_uuid"]
        assert admin_handler_calls == []

    async def test_an_unrecognised_uuid_is_refused(self, admin_handler_calls):
        ws = _Ws([{"type": MSG_ADMIN_HELLO, "machine_uuid": "NOT-LISTED",
                   "password": "hunter2"}])

        await rs._handler(ws)

        assert ws.reasons() == ["uuid_not_authorised"]
        assert admin_handler_calls == []

    async def test_the_refusal_echoes_the_uuid_so_it_can_be_authorised(self):
        """Deliberate: the operator needs the value to paste into the admin
        panel. Asserted so nobody 'tightens' it away without noticing that it
        makes granting access impossible from the failing machine."""
        ws = _Ws([{"type": MSG_ADMIN_HELLO, "machine_uuid": "NOT-LISTED",
                   "password": "hunter2"}])

        await rs._handler(ws)

        assert ws.sent[0]["machine_uuid"] == "NOT-LISTED"

    async def test_the_wrong_password_is_refused(self, monkeypatch,
                                                 admin_handler_calls):
        monkeypatch.setattr(rs, "_admin_machines", [{"uuid": "UUID-1"}])
        monkeypatch.setattr(rs, "is_admin_machine_uuid", lambda u: u == "UUID-1")
        monkeypatch.setattr(auth, "verify_password", lambda p: False)

        ws = _Ws([{"type": MSG_ADMIN_HELLO, "machine_uuid": "UUID-1",
                   "password": "wrong"}])

        await rs._handler(ws)

        assert ws.reasons() == ["wrong_password"]
        assert admin_handler_calls == [], "a wrong password reached the admin channel"

    async def test_an_authorised_uuid_with_the_right_password_gets_in(
            self, monkeypatch, admin_handler_calls):
        """The positive control. Without it, a handler that refused everyone
        would pass every test above."""
        monkeypatch.setattr(rs, "is_admin_machine_uuid", lambda u: u == "UUID-1")
        monkeypatch.setattr(auth, "verify_password", lambda p: True)

        ws = _Ws([{"type": MSG_ADMIN_HELLO, "machine_uuid": "UUID-1",
                   "password": "right"}])

        await rs._handler(ws)

        assert ws.types() == [MSG_ADMIN_WELCOME]
        assert admin_handler_calls == ["UUID-1"]

    async def test_the_password_is_checked_even_for_a_listed_uuid(
            self, monkeypatch, admin_handler_calls):
        """Being on the machine list is not authentication on its own."""
        monkeypatch.setattr(rs, "is_admin_machine_uuid", lambda u: True)
        monkeypatch.setattr(auth, "verify_password", lambda p: False)

        ws = _Ws([{"type": MSG_ADMIN_HELLO, "machine_uuid": "UUID-1",
                   "password": ""}])

        await rs._handler(ws)

        assert admin_handler_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Registration: queues, never grants
# ─────────────────────────────────────────────────────────────────────────────

class TestRegistration:

    async def test_it_queues_the_request_and_grants_nothing(self, no_side_effects):
        ws = _Ws([{"type": MSG_REGISTER, "token": "NEW-TOKEN",
                   "hostname": "simons-mac", "email": "a@b.c"}])

        await rs._handler(ws)

        assert "NEW-TOKEN" in rs._pending
        assert rs._pending["NEW-TOKEN"]["email"] == "a@b.c"
        assert rs._allowed_tokens == {}, "registration granted a token"
        assert ws.types() == [], "registration was answered as if approved"
        assert ws.closed is True

    async def test_it_records_where_the_request_came_from(self, no_side_effects):
        ws = _Ws([{"type": MSG_REGISTER, "token": "NEW-TOKEN"}], ip="198.51.100.7")

        await rs._handler(ws)

        assert rs._pending["NEW-TOKEN"]["ip"] == "198.51.100.7"

    async def test_an_empty_token_queues_nothing(self, no_side_effects):
        ws = _Ws([{"type": MSG_REGISTER, "token": ""}])

        await rs._handler(ws)

        assert rs._pending == {}

    async def test_an_ALREADY_APPROVED_token_is_not_pushed_back_into_pending(
            self, monkeypatch, no_side_effects):
        """The in-flight approval race. The client built this MSG_REGISTER
        before it knew an admin had approved it. Re-queueing would un-approve
        it in the admin UI while a valid licence already existed for it."""
        monkeypatch.setattr(rs, "_allowed_tokens", {"TOKEN-A": {"email": "a@b.c"}})

        ws = _Ws([{"type": MSG_REGISTER, "token": "TOKEN-A"}])

        await rs._handler(ws)

        assert rs._pending == {}
        assert rs._allowed_tokens["TOKEN-A"]["email"] == "a@b.c", "approval was lost"


# ─────────────────────────────────────────────────────────────────────────────
# Client HELLO
# ─────────────────────────────────────────────────────────────────────────────

class TestARevokedTokenAlwaysLearnsWhy:

    async def test_it_gets_the_revoke_notice(self, monkeypatch):
        monkeypatch.setattr(rs, "_revoked_tokens", {"DEAD"})

        ws = _Ws([{"type": MSG_HELLO, "token": "DEAD"}])

        await rs._handler(ws)

        assert ws.reasons() == ["revoked"]

    async def test_it_gets_it_EVEN_WHEN_RATE_LIMITED(self, monkeypatch):
        """Ordering, and it is deliberate. A revoked client reconnecting is
        expected traffic, not an attack -- and if the rate limiter answered
        first it would retry forever without ever being told the token is
        dead."""
        monkeypatch.setattr(rs, "_revoked_tokens", {"DEAD"})
        monkeypatch.setattr(rs, "_auth_failures",
                            {"203.0.113.9": [rs.time.time()] * 99})
        assert rs._is_rate_limited("203.0.113.9") is True, "the fixture is not limiting"

        ws = _Ws([{"type": MSG_HELLO, "token": "DEAD"}])

        await rs._handler(ws)

        assert ws.reasons() == ["revoked"]

    async def test_a_revoked_token_is_never_welcomed(self, monkeypatch):
        """Belt and braces: even if the token is somehow still in the allowed
        map, revoked wins."""
        monkeypatch.setattr(rs, "_revoked_tokens", {"DEAD"})
        monkeypatch.setattr(rs, "_allowed_tokens", {"DEAD": {"email": "a@b.c"}})

        ws = _Ws([{"type": MSG_HELLO, "token": "DEAD"}])

        await rs._handler(ws)

        assert MSG_WELCOME not in ws.types()


class TestRateLimiting:

    async def test_a_limited_ip_is_closed_without_an_answer(self, monkeypatch):
        monkeypatch.setattr(rs, "_auth_failures",
                            {"203.0.113.9": [rs.time.time()] * 99})

        ws = _Ws([{"type": MSG_HELLO, "token": "WHATEVER"}])

        await rs._handler(ws)

        assert ws.sent == []
        assert ws.closed is True

    async def test_an_unknown_token_is_NOT_recorded_as_a_failure(self):
        """A new install's first connection carries a token the server has
        never seen. Counting that as an auth failure would rate-limit ordinary
        users out of registering at all."""
        ws = _Ws([{"type": MSG_HELLO, "token": "BRAND-NEW"}])

        await rs._handler(ws)

        assert rs._auth_failures.get("203.0.113.9", []) == []


class TestAnUnknownTokenGetsASecondChance:

    async def test_it_is_told_the_token_is_invalid(self):
        ws = _Ws([{"type": MSG_HELLO, "token": "BRAND-NEW"}])

        await rs._handler(ws)

        assert ws.reasons() == ["invalid_token"]

    async def test_the_connection_stays_open_for_the_follow_up_register(
            self, no_side_effects):
        """Windows fingerprinting shells out per field and can take seconds, so
        the client's MSG_REGISTER arrives on the SAME connection after the
        rejection. Closing early drops the registration silently -- it reaches
        neither admin console, because it never reaches _pending at all."""
        ws = _Ws([
            {"type": MSG_HELLO, "token": "BRAND-NEW", "hostname": "simons-pc"},
            {"type": MSG_REGISTER, "token": "BRAND-NEW", "email": "a@b.c",
             "machine_id": "MACHINE-9"},
        ])

        await rs._handler(ws)

        assert "BRAND-NEW" in rs._pending
        assert rs._pending["BRAND-NEW"]["email"] == "a@b.c"
        assert rs._pending["BRAND-NEW"]["machine_id"] == "MACHINE-9"

    async def test_the_follow_up_falls_back_to_the_hello_values(
            self, no_side_effects):
        """The MSG_REGISTER may omit fields the HELLO carried."""
        ws = _Ws([
            {"type": MSG_HELLO, "token": "BRAND-NEW", "hostname": "simons-pc",
             "platform": "win32", "version": "1.2.3"},
            {"type": MSG_REGISTER, "token": "BRAND-NEW"},
        ])

        await rs._handler(ws)

        entry = rs._pending["BRAND-NEW"]
        assert entry["hostname"] == "simons-pc"
        assert entry["platform"] == "win32"
        assert entry["version"] == "1.2.3"

    async def test_a_successful_follow_up_clears_the_ips_failure_record(
            self, monkeypatch, no_side_effects):
        monkeypatch.setattr(rs, "_auth_failures", {"203.0.113.9": [rs.time.time()]})

        ws = _Ws([
            {"type": MSG_HELLO, "token": "BRAND-NEW"},
            {"type": MSG_REGISTER, "token": "BRAND-NEW"},
        ])

        await rs._handler(ws)

        assert "203.0.113.9" not in rs._auth_failures

    async def test_the_follow_up_does_not_un_approve_an_approved_token(
            self, monkeypatch, no_side_effects):
        """Same in-flight race as above, on the second-chance path."""
        monkeypatch.setattr(rs, "_allowed_tokens", {"TOKEN-A": {"email": "a@b.c"}})
        # Unknown at HELLO time is what gets us here, so the HELLO carries a
        # different token from the REGISTER -- exactly what happens when an
        # admin approves while the client is mid-fingerprint.
        ws = _Ws([
            {"type": MSG_HELLO, "token": "BRAND-NEW"},
            {"type": MSG_REGISTER, "token": "TOKEN-A"},
        ])

        await rs._handler(ws)

        assert rs._pending == {}
        assert rs._allowed_tokens["TOKEN-A"]["email"] == "a@b.c"

    async def test_a_follow_up_that_never_arrives_just_ends(self):
        ws = _Ws([{"type": MSG_HELLO, "token": "BRAND-NEW"}])

        await rs._handler(ws)

        assert rs._pending == {}


class TestTheTimeoutsAreActuallyBounded:
    """The tests above exercise the timeout BRANCHES without waiting. These
    assert the bounds themselves, so shrinking the fake cannot quietly turn an
    unbounded `recv()` into a passing suite.

    Read from the source rather than called, because calling them is exactly
    the 25 seconds this avoids.
    """

    def _wait_for_timeouts(self) -> list[float]:
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(rs._handler)))
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "wait_for"):
                for kw in node.keywords:
                    if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                        out.append(float(kw.value.value))
        return out

    async def test_every_recv_in_the_handler_is_bounded(self):
        timeouts = self._wait_for_timeouts()

        assert len(timeouts) == 2, (
            f"expected the auth wait and the registration follow-up wait, "
            f"found {timeouts}. An unbounded recv() holds a connection slot "
            f"open forever."
        )
        assert all(t > 0 for t in timeouts)

    async def test_the_auth_wait_is_short_and_the_follow_up_wait_is_not(self):
        """The follow-up has to outlast Windows fingerprinting, which shells
        out per field with a 6s timeout each. The auth wait has no reason to be
        anything but short."""
        auth_wait, follow_up_wait = self._wait_for_timeouts()

        assert auth_wait <= 10.0
        assert follow_up_wait >= 15.0
