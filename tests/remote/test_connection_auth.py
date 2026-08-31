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

    def __aiter__(self):
        """The authenticated session loop reads with `async for`."""
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        frame = self._frames.pop(0)
        return frame if isinstance(frame, (str, bytes)) else json.dumps(frame)

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


# ─────────────────────────────────────────────────────────────────────────────
# The authenticated session
# ─────────────────────────────────────────────────────────────────────────────

from backend.src.services.cluster.remote.protocol import (   # noqa: E402
    MSG_LICENCE, MSG_STATUS, MSG_VERSION_INFO,
)


@pytest.fixture
def known_client(monkeypatch):
    """One approved token, and the admin fan-out silenced."""
    monkeypatch.setattr(rs, "_allowed_tokens", {
        "GOOD": {"name": "Simon's VPS", "email": "a@b.c",
                 "subscription_type": "Annual", "expiry_date": "2027-01-01"},
    })

    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(rs, "_push_clients_to_all_admins", _noop)
    return rs._allowed_tokens["GOOD"]


class TestTheWelcomeSequence:
    """This is the path bugs/018 broke.

    `_read_changelog` is called here, on every successful connection. When the
    2026-08-30 split left `_CHANGELOG_FILE` behind, this raised NameError after
    the welcome and the licence had already been sent -- so a client was
    welcomed, licensed, and then dropped before it learned what version to
    update to. These tests exist so that cannot happen unnoticed again; the
    static gate (tests/refactor/test_undefined_names.py) is the other half.
    """

    async def test_a_known_token_gets_welcome_then_version_info(self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD", "hostname": "vps-1"}])

        await rs._handler(ws)

        assert ws.types() == [MSG_WELCOME, MSG_VERSION_INFO]

    async def test_the_version_info_carries_a_version_and_a_changelog(
            self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}])

        await rs._handler(ws)

        info = next(m for m in ws.sent if m["type"] == MSG_VERSION_INFO)
        assert info["latest"], "no version advertised -- clients cannot self-update"
        assert isinstance(info["changelog"], list)

    async def test_the_welcome_carries_the_subscription_the_token_holds(
            self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}])

        await rs._handler(ws)

        welcome = ws.sent[0]
        assert welcome["subscription_type"] == "Annual"
        assert welcome["expiry_date"] == "2027-01-01"
        assert welcome["email"] == "a@b.c"

    async def test_a_licence_is_delivered_when_the_token_has_one(
            self, known_client):
        known_client["licence_key"] = "LICENCE-XYZ"
        known_client["machine_id"] = "MACHINE-9"

        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}])

        await rs._handler(ws)

        assert ws.types() == [MSG_WELCOME, MSG_LICENCE, MSG_VERSION_INFO]
        licence = ws.sent[1]
        assert licence["licence_key"] == "LICENCE-XYZ"
        assert licence["machine_id"] == "MACHINE-9"

    async def test_no_licence_frame_when_the_token_has_none(self, known_client):
        """Control for the test above -- otherwise a handler that always sent
        a licence frame, empty or not, would look correct."""
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}])

        await rs._handler(ws)

        assert MSG_LICENCE not in ws.types()

    async def test_an_admin_machine_is_told_it_is_one(self, known_client,
                                                      monkeypatch):
        monkeypatch.setattr(rs, "is_admin_machine_uuid", lambda u: u == "UUID-1")

        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD", "machine_uuid": "UUID-1"}])

        await rs._handler(ws)

        assert ws.sent[0]["is_remote_admin"] is True

    async def test_an_ordinary_machine_is_not(self, known_client, monkeypatch):
        monkeypatch.setattr(rs, "is_admin_machine_uuid", lambda u: False)

        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD", "machine_uuid": "UUID-9"}])

        await rs._handler(ws)

        assert ws.sent[0]["is_remote_admin"] is False


class TestTheSessionIsRegisteredAndCleanedUp:

    async def test_a_disconnect_removes_the_client(self, known_client):
        """The `finally` block. A session left in `_connected` after the socket
        dies shows the client as online forever on every admin screen."""
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD", "hostname": "vps-1"}])

        await rs._handler(ws)

        assert "GOOD" not in rs._connected

    async def test_the_connection_is_registered_while_it_lasts(
            self, known_client, monkeypatch):
        """Positive control for the cleanup test: proves the entry was ever
        there. Captured mid-session, since by the time _handler returns the
        `finally` has already removed it."""
        seen: list = []

        async def _capture(*_a, **_kw):
            seen.append(dict(rs._connected))
        monkeypatch.setattr(rs, "_push_clients_to_all_admins", _capture)

        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD", "hostname": "vps-1"}])

        await rs._handler(ws)

        assert seen and "GOOD" in seen[0]
        assert seen[0]["GOOD"]["info"]["hostname"] == "vps-1"
        assert seen[0]["GOOD"]["info"]["online"] is True

    async def test_the_tokens_last_seen_is_stamped(self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}])

        await rs._handler(ws)

        assert rs._allowed_tokens["GOOD"]["last_seen"] > 0


class TestTheStatusHeartbeat:

    async def test_it_updates_what_the_admin_screens_show(self, known_client):
        """A client that updates itself mid-session reports the new build on
        its next heartbeat, not on a fresh HELLO -- so the admin list has to
        pick it up from here or it shows the old version until reconnect."""
        snapshots: list = []

        class _Watching(_Ws):
            async def __anext__(self):
                # Snapshot BEFORE serving the next frame, so the effect of the
                # previous one is visible while the session still exists.
                entry = rs._connected.get("GOOD")
                if entry:
                    snapshots.append(dict(entry["info"]))
                return await super().__anext__()

        ws = _Watching([
            {"type": MSG_HELLO, "token": "GOOD", "version": "1.0.0"},
            {"type": MSG_STATUS, "version": "1.1.0", "trades_open": 3,
             "bridge_connected": True, "uptime_s": 120},
        ])
        ws._frames.insert(0, {"type": MSG_HELLO, "token": "GOOD",
                              "version": "1.0.0"})

        await rs._handler(ws)

        assert snapshots, "the session was never registered"
        before, after = snapshots[0], snapshots[-1]
        assert before["version"] == "1.0.0"
        assert after["version"] == "1.1.0", "a mid-session self-update was not picked up"
        assert after["trades_open"] == 3
        assert after["bridge_connected"] is True
        assert after["uptime_s"] == 120

    async def test_the_uptime_accumulator_caps_a_long_gap(self, known_client,
                                                          monkeypatch):
        """A missed heartbeat, or a reconnect after the machine slept, must not
        book hours of "online" that never happened. The cap is 90 seconds per
        gap, and this figure is persisted and shown to the operator."""
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(rs.time, "time", lambda: clock["t"])

        class _Jumpy(_Ws):
            """A full day passes between the two heartbeats."""
            async def __anext__(self):
                frame = await super().__anext__()
                clock["t"] += 86400.0
                return frame

        ws = _Jumpy([
            {"type": MSG_HELLO, "token": "GOOD"},
            {"type": MSG_STATUS},      # first: sets the baseline, adds nothing
            {"type": MSG_STATUS},      # second: a day later
        ])

        await rs._handler(ws)

        total = rs._allowed_tokens["GOOD"].get("total_uptime_s", 0)
        assert total > 0, "no uptime was accumulated at all -- test proves nothing"
        assert total <= 90.0, (
            f"booked {total}s from a one-day gap. The 90s cap is what stops a "
            f"sleeping machine inflating its recorded time online."
        )

    async def test_a_normal_gap_is_counted_in_full(self, known_client,
                                                   monkeypatch):
        """Control for the cap: an ordinary ~60s heartbeat interval must be
        added whole. A cap that clamped everything would pass the test above
        and make the figure useless."""
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(rs.time, "time", lambda: clock["t"])

        class _Ticking(_Ws):
            async def __anext__(self):
                frame = await super().__anext__()
                clock["t"] += 60.0
                return frame

        ws = _Ticking([
            {"type": MSG_HELLO, "token": "GOOD"},
            {"type": MSG_STATUS},
            {"type": MSG_STATUS},
        ])

        await rs._handler(ws)

        assert rs._allowed_tokens["GOOD"].get("total_uptime_s") == 60.0

    async def test_a_binary_frame_is_ignored(self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}, b"\\x00\\x01"])

        await rs._handler(ws)   # must not raise

        assert ws.types() == [MSG_WELCOME, MSG_VERSION_INFO]

    async def test_malformed_json_mid_session_is_ignored(self, known_client):
        ws = _Ws([{"type": MSG_HELLO, "token": "GOOD"}, "{broken"])

        await rs._handler(ws)   # must not raise

        assert "GOOD" not in rs._connected


class TestRevocationMidSession:
    async def test_a_token_revoked_while_connected_does_not_crash_the_loop(
            self, known_client):
        """`_allowed_tokens[token]` is indexed on every message, and an admin
        can revoke between two of them."""
        class _Revoking(_Ws):
            async def __anext__(self):
                rs._allowed_tokens.pop("GOOD", None)
                return await super().__anext__()

        ws = _Revoking([
            {"type": MSG_HELLO, "token": "GOOD"},
            {"type": MSG_STATUS},
        ])
        ws._frames.insert(0, {"type": MSG_HELLO, "token": "GOOD"})

        await rs._handler(ws)   # must not raise

        assert "GOOD" not in rs._connected
