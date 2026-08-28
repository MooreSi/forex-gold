"""What a connected remote admin can actually do.

`tests/remote/test_admin_auth.py` covers whether someone gets IN (password) and
`test_licence_lifecycle.py` covers the grant/revoke primitives. This is the
layer between: the command handler an authenticated admin machine drives over
its websocket -- approve, reject, revoke, issue a licence, and the licence-DB
operations.

Two things matter more than the individual commands:

  * Every branch must REPLY. The admin UI drives these from a screen with a
    spinner on it; a command that returns without a result leaves the operator
    looking at a hung dialog, unable to tell "refused" from "not delivered".
  * A command from a uuid with no live admin session must do NOTHING. That
    lookup is the last check standing between a message and licence issuance.

No socket. The admin's websocket is a fake that records frames, and the
licence-DB callbacks are recorded rather than run.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.remote import server as rs
from backend.src.services.cluster.remote.protocol import (
    MSG_ADMIN_APPROVE, MSG_ADMIN_REJECT, MSG_ADMIN_REVOKE,
    MSG_ADMIN_ISSUE, MSG_ADMIN_DB_OP, MSG_ADMIN_RESULT,
)


pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
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
    monkeypatch.setattr(rs, "_admin_machines", [])
    monkeypatch.setattr(rs, "_auth_failures", {})
    monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"LICENCE-{mid}")
    monkeypatch.setattr(rs, "_kg_insert_fn", None)
    monkeypatch.setattr(rs, "_kg_revoke_fn", None)
    monkeypatch.setattr(rs, "_kg_reinstate_fn", None)
    monkeypatch.setattr(rs, "_kg_delete_fn", None)
    return d


@pytest.fixture
def admin(monkeypatch):
    """A connected admin session for UUID-1."""
    ws = _Ws()
    monkeypatch.setattr(rs, "_admin_clients", {"UUID-1": {"ws": ws}})
    return ws


def _pending_entry(**over):
    e = {"hostname": "simons-mac", "email": "a@b.c", "nickname": "Simon",
         "platform": "darwin", "machine_id": "MACHINE-1", "ip": "203.0.113.9"}
    e.update(over)
    return e


def _results(ws):
    return [f for f in ws.sent if f.get("type") == MSG_ADMIN_RESULT]


class TestNoLiveSession:
    async def test_a_command_from_an_unknown_uuid_does_nothing(self, monkeypatch):
        """The last check between a message and licence issuance. There is no
        session to reply to, so the only correct behaviour is silence AND no
        state change."""
        monkeypatch.setattr(rs, "_admin_clients", {})
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon"}, "UUID-EVIL")

        assert rs._allowed_tokens == {}
        assert "tok" in rs._pending

    async def test_a_session_entry_with_no_websocket_does_nothing(self, monkeypatch):
        monkeypatch.setattr(rs, "_admin_clients", {"UUID-1": {}})
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon"}, "UUID-1")

        assert rs._allowed_tokens == {}


class TestApprove:
    async def test_it_approves_and_reports_success(self, admin):
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon",
             "subscription_type": "1 Year"}, "UUID-1")

        assert "tok" in rs._allowed_tokens
        assert rs._allowed_tokens["tok"]["subscription_type"] == "1 Year"
        assert _results(admin)[0]["ok"] is True

    async def test_an_unknown_token_reports_FAILURE_rather_than_silence(self, admin):
        """The operator is watching a spinner. Silence here is
        indistinguishable from a dropped connection."""
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "never-seen", "name": "X"}, "UUID-1")

        res = _results(admin)
        assert len(res) == 1
        assert res[0]["ok"] is False
        assert "pending" in res[0]["msg"].lower()

    async def test_the_result_names_the_command_it_answers(self, admin):
        """Several commands can be in flight from one screen. Without cmd the
        UI cannot tell which reply belongs to which button."""
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "x", "name": "X"}, "UUID-1")

        assert _results(admin)[0]["cmd"] == MSG_ADMIN_APPROVE

    async def test_the_licence_db_gets_the_record(self, admin, monkeypatch):
        inserted = []
        monkeypatch.setattr(rs, "_kg_insert_fn", lambda rec: inserted.append(rec))
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon"}, "UUID-1")

        assert len(inserted) == 1
        rec = inserted[0]
        # The machine id goes in as registration_id -- licences.db's own name
        # for it, not the token metadata's. Asserted explicitly because the
        # two names sit either side of one mapping and nothing else checks it.
        assert rec["registration_id"] == "MACHINE-1"
        assert rec["licence_key"] == "LICENCE-MACHINE-1"
        assert rec["email"] == "a@b.c"
        assert rec["nickname"] == "Simon"
        assert rec["hostname"] == "simons-mac"

    async def test_a_FAILING_licence_db_does_not_undo_the_approval(self, admin, monkeypatch):
        """The token is already granted and saved by this point. Reporting
        failure would have the operator approve again; the licences.db row is
        a secondary record and is logged when it fails."""
        def _boom(rec):
            raise RuntimeError("licences.db locked")
        monkeypatch.setattr(rs, "_kg_insert_fn", _boom)
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon"}, "UUID-1")

        assert "tok" in rs._allowed_tokens
        assert _results(admin)[0]["ok"] is True

    async def test_it_defaults_to_perpetual_when_none_is_given(self, admin):
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_APPROVE, "token": "tok", "name": "Simon"}, "UUID-1")

        assert rs._allowed_tokens["tok"]["subscription_type"] == "Perpetual"


class TestReject:
    async def test_it_drops_the_pending_registration(self, admin):
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REJECT, "token": "tok"}, "UUID-1")

        assert "tok" not in rs._pending
        assert _results(admin)[0]["ok"] is True

    async def test_rejecting_GRANTS_NOTHING(self, admin):
        """The obvious one, and worth stating: reject must not leave the token
        anywhere that later reads as approved."""
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REJECT, "token": "tok"}, "UUID-1")

        assert rs._allowed_tokens == {}

    async def test_it_is_persisted(self, admin, isolated):
        """Otherwise a restart brings the rejected request back into the
        approval queue."""
        rs._pending["tok"] = _pending_entry()

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REJECT, "token": "tok"}, "UUID-1")

        assert json.loads((isolated / "pending_registrations.json").read_text()) == {}

    async def test_an_unknown_token_reports_failure(self, admin):
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REJECT, "token": "never-seen"}, "UUID-1")

        assert _results(admin)[0]["ok"] is False


class TestRevoke:
    async def test_it_revokes_and_reports_success(self, admin):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REVOKE, "token": "tok"}, "UUID-1")

        assert "tok" not in rs._allowed_tokens
        assert "tok" in rs._revoked_tokens
        assert _results(admin)[0]["ok"] is True

    async def test_revoking_an_unknown_token_still_reports_success(self, admin):
        """Deliberate, and it matches revoke_token(): the token goes on the
        revoke list regardless, so the outcome the operator asked for -- that
        this token cannot connect -- is true either way."""
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_REVOKE, "token": "never-seen"}, "UUID-1")

        assert _results(admin)[0]["ok"] is True
        assert "never-seen" in rs._revoked_tokens


class TestIssueALicence:
    async def test_it_refuses_an_empty_record(self, admin, monkeypatch):
        inserted = []
        monkeypatch.setattr(rs, "_kg_insert_fn", lambda rec: inserted.append(rec))

        await rs._handle_admin_command({"type": MSG_ADMIN_ISSUE, "record": {}}, "UUID-1")

        assert inserted == []
        assert _results(admin)[0]["ok"] is False

    async def test_it_refuses_when_the_licence_db_is_unavailable(self, admin):
        """No callback registered means nothing would be persisted. Reporting
        success would have the operator believe a licence exists."""
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_ISSUE, "record": {"email": "a@b.c"}}, "UUID-1")

        res = _results(admin)[0]
        assert res["ok"] is False
        assert "unavailable" in res["msg"].lower()

    async def test_it_issues_when_the_db_is_there(self, admin, monkeypatch):
        inserted = []
        monkeypatch.setattr(rs, "_kg_insert_fn", lambda rec: inserted.append(rec))

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_ISSUE, "record": {"email": "a@b.c"}}, "UUID-1")

        assert inserted == [{"email": "a@b.c"}]
        assert _results(admin)[0]["ok"] is True

    async def test_a_failing_insert_reports_the_reason(self, admin, monkeypatch):
        def _boom(rec):
            raise RuntimeError("duplicate email")
        monkeypatch.setattr(rs, "_kg_insert_fn", _boom)

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_ISSUE, "record": {"email": "a@b.c"}}, "UUID-1")

        res = _results(admin)[0]
        assert res["ok"] is False
        assert "duplicate email" in res["msg"]


class TestLicenceDbOps:
    async def test_an_unknown_op_is_refused(self, admin):
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_DB_OP, "op": "drop_everything", "id": 1}, "UUID-1")

        assert _results(admin)[0]["ok"] is False

    async def test_an_op_with_NO_CALLBACK_REGISTERED_is_refused(self, admin):
        """Same branch as the unknown op, and that is the point: a valid op
        name with no callback behind it must not silently look like it worked.
        The message says 'or DB unavailable' for exactly this."""
        await rs._handle_admin_command(
            {"type": MSG_ADMIN_DB_OP, "op": "delete", "id": 1}, "UUID-1")

        res = _results(admin)[0]
        assert res["ok"] is False
        assert "unavailable" in res["msg"].lower()

    @pytest.mark.parametrize("op,attr,label", [
        ("revoke", "_kg_revoke_fn", "Revoked"),
        ("reinstate", "_kg_reinstate_fn", "Reinstated"),
        ("delete", "_kg_delete_fn", "Deleted"),
    ])
    async def test_each_op_calls_its_own_callback(self, admin, monkeypatch,
                                                  op, attr, label):
        """A crossed mapping would delete a licence the operator asked to
        reinstate."""
        seen = []
        monkeypatch.setattr(rs, attr, lambda lid: seen.append(lid) or True)

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_DB_OP, "op": op, "id": 42}, "UUID-1")

        assert seen == [42], f"{op} did not call {attr}"
        res = _results(admin)[0]
        assert res["ok"] is True and res["msg"] == label

    async def test_a_callback_returning_false_reports_failure(self, admin, monkeypatch):
        monkeypatch.setattr(rs, "_kg_delete_fn", lambda lid: False)

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_DB_OP, "op": "delete", "id": 42}, "UUID-1")

        res = _results(admin)[0]
        assert res["ok"] is False
        assert res["msg"] == "Could not delete"

    async def test_a_raising_callback_reports_the_reason(self, admin, monkeypatch):
        def _boom(lid):
            raise RuntimeError("row is locked")
        monkeypatch.setattr(rs, "_kg_delete_fn", _boom)

        await rs._handle_admin_command(
            {"type": MSG_ADMIN_DB_OP, "op": "delete", "id": 42}, "UUID-1")

        assert "row is locked" in _results(admin)[0]["msg"]


class TestEveryCommandReplies:
    @pytest.mark.parametrize("msg", [
        {"type": MSG_ADMIN_APPROVE, "token": "nope", "name": "X"},
        {"type": MSG_ADMIN_REJECT, "token": "nope"},
        {"type": MSG_ADMIN_REVOKE, "token": "nope"},
        {"type": MSG_ADMIN_ISSUE, "record": {}},
        {"type": MSG_ADMIN_DB_OP, "op": "delete", "id": 1},
    ])
    async def test_it_replies_exactly_once(self, admin, msg):
        """The admin UI shows a spinner per command. A missing reply hangs it;
        two replies resolve the wrong one."""
        await rs._handle_admin_command(msg, "UUID-1")

        assert len(_results(admin)) == 1, f"{msg['type']} did not reply exactly once"

    async def test_an_unrecognised_command_is_ignored_without_a_reply(self, admin):
        """No branch matches, so nothing is sent. Recorded as the current
        behaviour: the UI only ever sends the five types above, so this is
        reachable only from a malformed or future client."""
        await rs._handle_admin_command({"type": "admin_something_new"}, "UUID-1")

        assert _results(admin) == []
