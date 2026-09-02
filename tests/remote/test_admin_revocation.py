"""Revoking a machine, and pushing a licence to one.

`revoke_token` is the most consequential thing an admin can do to a client:
it removes the token, remembers the revocation across restarts, and closes the
live connection. Every one of those steps carries a way to fail quietly.

  * **Forgetting it.** `_revoked_tokens` is what stops a revoked client
    re-registering straight back in. A revocation that is not remembered
    across a restart is not a revocation, it is a disconnection.
  * **Leaving it in the pending queue.** A revoked machine sitting in
    `_pending` invites the admin to approve it again from the console.
  * **The rate limiter.** The revoked client reconnects immediately to receive
    its notice, and its IP's failure count is cleared so the limiter does not
    block the one connection that tells it it has been revoked.

`_send_licence` is the opposite direction, and its failure mode is the pair to
bugs/014's: it must not raise, because it is fired as a background task from
the startup re-sign and an escaping exception there is invisible.

No sockets and no files: the token store is a dict and every save is captured.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.remote import server as rs
from backend.src.services.cluster.remote.protocol import MSG_LICENCE, MSG_REVOKE

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self, fails=False):
        self.sent: list = []
        self.closed = False
        self.fails = fails

    async def send(self, raw):
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def store(monkeypatch):
    saves: list = []
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_revoked_tokens", set())
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_auth_failures", {})
    for name in ("_save_tokens", "_save_pending", "_save_revoked"):
        monkeypatch.setattr(rs, name, lambda n=name: saves.append(n))
    return saves


class TestRevoking:
    async def test_the_token_stops_being_allowed(self, store):
        rs._allowed_tokens["t1"] = {"name": "mac"}

        rs.revoke_token("t1")

        assert "t1" not in rs._allowed_tokens

    async def test_it_is_remembered_across_restarts(self, store):
        """The list is what stops a revoked client re-registering straight
        back in. Not remembering it makes this a disconnection, not a
        revocation."""
        rs._allowed_tokens["t1"] = {"name": "mac"}

        rs.revoke_token("t1")

        assert "t1" in rs._revoked_tokens
        assert "_save_revoked" in store

    async def test_it_is_dropped_from_the_pending_queue(self, store):
        """A revoked machine left pending invites the admin to approve it
        again from the console."""
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._pending["t1"] = {"hostname": "mac"}

        rs.revoke_token("t1")

        assert "t1" not in rs._pending

    async def test_all_three_stores_are_written(self, store):
        rs._allowed_tokens["t1"] = {"name": "mac"}

        rs.revoke_token("t1")

        assert set(store) == {"_save_tokens", "_save_pending", "_save_revoked"}

    async def test_revoking_an_unknown_token_is_harmless(self, store):
        """The admin console can be a moment behind the truth."""
        rs.revoke_token("never-existed")

        assert "never-existed" in rs._revoked_tokens


class TestRevokingAConnectedClient:
    async def test_the_client_is_told(self, store):
        ws = _Ws()
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": ws, "info": {"ip": "1.2.3.4"}}

        rs.revoke_token("t1")
        await asyncio.sleep(0)

        assert MSG_REVOKE in ws.types()

    async def test_the_connection_is_closed(self, store):
        ws = _Ws()
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": ws, "info": {"ip": "1.2.3.4"}}

        rs.revoke_token("t1")
        await asyncio.sleep(0)

        assert ws.closed

    async def test_the_rate_limiter_is_cleared_for_that_ip(self, store):
        """The revoked client reconnects at once to receive its notice. The
        limiter must not block the one connection that tells it."""
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": _Ws(), "info": {"ip": "1.2.3.4"}}
        rs._auth_failures["1.2.3.4"] = 5

        rs.revoke_token("t1")

        assert "1.2.3.4" not in rs._auth_failures

    async def test_another_ip_is_not_cleared(self, store):
        """Negative control: clearing the whole table would let every
        rate-limited attacker through on any revocation."""
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": _Ws(), "info": {"ip": "1.2.3.4"}}
        rs._auth_failures.update({"1.2.3.4": 5, "9.9.9.9": 5})

        rs.revoke_token("t1")

        assert rs._auth_failures.get("9.9.9.9") == 5

    async def test_the_entry_is_left_for_the_handler_to_clean_up(self, store):
        """Popped here, an in-flight message in the handler would find no
        entry. The handler's finally block owns that removal."""
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": _Ws(), "info": {}}

        rs.revoke_token("t1")

        assert "t1" in rs._connected

    async def test_a_dead_socket_still_completes_the_revocation(self, store):
        """The token is gone whether or not the notice lands."""
        rs._allowed_tokens["t1"] = {"name": "mac"}
        rs._connected["t1"] = {"ws": _Ws(fails=True), "info": {}}

        rs.revoke_token("t1")
        await asyncio.sleep(0)

        assert "t1" in rs._revoked_tokens


class TestPushingALicence:
    async def test_it_carries_the_stored_licence(self, store):
        rs._allowed_tokens["t1"] = {
            "licence_key": "KEY", "expiry_date": "2027-01-01",
            "subscription_type": "1 Year", "machine_id": "M1",
            "email": "a@b.c", "name": "mac"}
        ws = _Ws()

        await rs._send_licence(ws, "t1")

        sent = ws.sent[0]
        assert sent["type"] == MSG_LICENCE
        assert sent["licence_key"] == "KEY"
        assert sent["machine_id"] == "M1"
        assert sent["licence_type"] == "1 Year"

    async def test_the_licence_type_defaults_to_perpetual(self, store):
        """An older token record has no subscription_type. Sending an empty
        one would show as a blank licence on the client."""
        rs._allowed_tokens["t1"] = {"licence_key": "KEY"}
        ws = _Ws()

        await rs._send_licence(ws, "t1")

        assert ws.sent[0]["licence_type"] == "Perpetual"

    async def test_an_unknown_token_does_not_raise(self, store):
        ws = _Ws()

        await rs._send_licence(ws, "never-existed")

    async def test_a_dead_socket_does_not_raise(self, store):
        """Fired as a background task from the startup re-sign. An escaping
        exception there is an unretrieved task exception -- invisible."""
        rs._allowed_tokens["t1"] = {"licence_key": "KEY"}

        await rs._send_licence(_Ws(fails=True), "t1")
