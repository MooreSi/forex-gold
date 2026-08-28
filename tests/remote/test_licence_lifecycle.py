"""Licence issuance, revocation, and who counts as an admin.

The largest untested surface in the app, and not an incidental one: this is
who is allowed to connect, what licence they are granted, and who can take it
away. `docs/todo/testing/012-cluster-tests.md` asks for the refusals before the
happy paths, so that is the order here.

Three properties are worth more than the rest:

  * An unknown token grants NOTHING. approve_registration() is reached from the
    Telegram approval path, where the token is whatever arrived in a message.
  * Revocation is REMEMBERED. It is written to disk, because a server restart
    that forgot it would silently reinstate every revoked client.
  * An empty machine UUID is NOT an admin. `is_admin_machine_uuid("")` guards
    the admin websocket; a client that failed to identify itself must not fall
    through into admin authority.

Every path is redirected into tmp_path and every module global is reset around
each test -- this module keeps its state in module-level dicts, so a leaked
token would make the next test lie. No socket is opened.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.src.services.cluster.remote import server as rs


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Private files and a clean slate of globals for one test."""
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
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_auth_failures", {})
    monkeypatch.setattr(rs, "_kg_sign_fn", None)
    return d


def _pending_entry(**over):
    entry = {"hostname": "simons-mac", "email": "a@b.c", "nickname": "Simon",
             "platform": "darwin", "machine_id": "MACHINE-1", "ip": "203.0.113.9"}
    entry.update(over)
    return entry


@pytest.fixture
def signing(monkeypatch):
    """A registered signing callback that records what it was asked to sign."""
    calls = []

    def _sign(machine_id, expiry):
        calls.append((machine_id, expiry))
        return f"LICENCE-{machine_id}-{expiry}"

    monkeypatch.setattr(rs, "_kg_sign_fn", _sign)
    return calls


class TestApprovalRefuses:
    def test_an_unknown_token_grants_nothing(self, signing):
        """Reached from the Telegram approval path, where the token is
        whatever arrived in a message."""
        assert rs.approve_registration("never-seen", "Someone") is False
        assert rs._allowed_tokens == {}
        assert signing == [], "a licence was signed for an unknown token"

    def test_an_already_approved_token_is_not_re_approvable(self, signing):
        """Approval consumes the pending entry, so a replayed approval
        message cannot re-issue or extend a licence."""
        rs._pending["tok"] = _pending_entry()
        assert rs.approve_registration("tok", "Simon", "1 Year") is True

        assert rs.approve_registration("tok", "Simon", "3 Years") is False
        assert rs._allowed_tokens["tok"]["subscription_type"] == "1 Year"

    def test_no_signing_callback_still_approves_but_with_NO_licence_key(self):
        """Records real behaviour, and it is surprising: with kg_sign_fn
        unregistered the token is added to the allowed list carrying an empty
        licence_key. It logs an error and continues rather than refusing.

        The client is then trusted by the server but holds nothing that
        validates offline. Pinned rather than changed -- refusing here is a
        licensing policy decision, not a test's to make."""
        rs._pending["tok"] = _pending_entry()

        assert rs.approve_registration("tok", "Simon") is True

        assert rs._allowed_tokens["tok"]["licence_key"] == ""

    def test_a_failing_signer_does_not_abort_the_approval_either(self, monkeypatch):
        def _boom(machine_id, expiry):
            raise RuntimeError("keygen unavailable")
        monkeypatch.setattr(rs, "_kg_sign_fn", _boom)
        rs._pending["tok"] = _pending_entry()

        assert rs.approve_registration("tok", "Simon") is True
        assert rs._allowed_tokens["tok"]["licence_key"] == ""

    def test_no_machine_id_means_no_licence_is_signed(self, signing):
        """The licence is bound to the machine. Signing without one would
        produce a key that validates anywhere."""
        rs._pending["tok"] = _pending_entry(machine_id="")

        rs.approve_registration("tok", "Simon")

        assert signing == []
        assert rs._allowed_tokens["tok"]["licence_key"] == ""


class TestApprovalGrants:
    def test_it_signs_for_the_machine_and_stores_the_key(self, signing):
        rs._pending["tok"] = _pending_entry()

        rs.approve_registration("tok", "Simon", "1 Year")

        assert len(signing) == 1
        machine_id, expiry = signing[0]
        assert machine_id == "MACHINE-1"
        assert rs._allowed_tokens["tok"]["licence_key"] == f"LICENCE-MACHINE-1-{expiry}"

    @pytest.mark.parametrize("sub,days", [
        ("6 Months", 183), ("1 Year", 365), ("2 Years", 730), ("3 Years", 1095),
    ])
    def test_the_expiry_matches_the_subscription(self, signing, sub, days):
        from datetime import datetime, timedelta
        rs._pending["tok"] = _pending_entry()

        rs.approve_registration("tok", "Simon", sub)

        expected = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        assert rs._allowed_tokens["tok"]["expiry_date"] == expected

    def test_perpetual_is_the_word_not_a_date(self, signing):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon", "Perpetual")
        assert rs._allowed_tokens["tok"]["expiry_date"] == "perpetual"

    def test_an_UNRECOGNISED_subscription_falls_back_to_perpetual(self, signing):
        """Worth knowing: a typo in the subscription type does not fail, it
        grants a licence that never expires."""
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon", "1 Yeer")
        assert rs._allowed_tokens["tok"]["expiry_date"] == "perpetual"

    def test_the_pending_entrys_details_are_carried_over(self, signing):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")
        entry = rs._allowed_tokens["tok"]
        assert entry["email"] == "a@b.c"
        assert entry["nickname"] == "Simon"
        assert entry["hostname"] == "simons-mac"
        assert entry["machine_id"] == "MACHINE-1"

    def test_the_display_name_falls_back_to_the_hostname(self, signing):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "")
        assert rs._allowed_tokens["tok"]["name"] == "simons-mac"

    def test_approval_clears_the_ips_failure_count(self, signing):
        """Otherwise a client that was rejected while pending stays rate
        limited and cannot collect the licence just granted to it."""
        rs._auth_failures["203.0.113.9"] = [time.time()] * 5
        rs._pending["tok"] = _pending_entry()

        rs.approve_registration("tok", "Simon")

        assert "203.0.113.9" not in rs._auth_failures


class TestRevocation:
    def test_it_removes_the_token_and_remembers_it(self, signing):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")

        rs.revoke_token("tok")

        assert "tok" not in rs._allowed_tokens
        assert "tok" in rs._revoked_tokens

    def test_the_revocation_IS_WRITTEN_TO_DISK(self, signing, isolated):
        """A restart that forgot this would silently reinstate every revoked
        client, since the allowed list is the only other record."""
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")

        rs.revoke_token("tok")

        assert json.loads((isolated / "revoked_tokens.json").read_text()) == ["tok"]
        assert json.loads((isolated / "allowed_tokens.json").read_text()) == {}

    def test_it_survives_a_reload(self, signing, isolated):
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")
        rs.revoke_token("tok")

        rs._allowed_tokens, rs._pending, rs._revoked_tokens = {}, {}, set()
        rs._load_tokens()

        assert rs._revoked_tokens == {"tok"}
        assert "tok" not in rs._allowed_tokens

    def test_it_also_clears_a_re_registration_attempt(self, signing):
        """A revoked client re-registering while the owner revokes must not
        leave a pending row that can be approved by mistake."""
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")
        rs._pending["tok"] = _pending_entry()

        rs.revoke_token("tok")

        assert "tok" not in rs._pending

    def test_revoking_an_unknown_token_is_harmless_and_still_remembered(self):
        """Called from the admin UI against whatever is selected."""
        rs.revoke_token("never-seen")
        assert "never-seen" in rs._revoked_tokens

    def test_RE_APPROVAL_LIFTS_the_revocation(self, signing):
        """Deliberate: an owner re-approving a client they revoked must not
        leave it permanently blocked by a stale entry in the revoke list."""
        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")
        rs.revoke_token("tok")

        rs._pending["tok"] = _pending_entry()
        rs.approve_registration("tok", "Simon")

        assert "tok" not in rs._revoked_tokens
        assert "tok" in rs._allowed_tokens


class TestRateLimiting:
    def test_a_fresh_ip_is_not_limited(self):
        assert rs._is_rate_limited("203.0.113.9") is False

    def test_four_failures_are_not_enough(self):
        for _ in range(4):
            rs._record_failure("203.0.113.9")
        assert rs._is_rate_limited("203.0.113.9") is False

    def test_the_fifth_failure_trips_it(self):
        for _ in range(5):
            rs._record_failure("203.0.113.9")
        assert rs._is_rate_limited("203.0.113.9") is True

    def test_the_window_slides(self):
        """Old failures age out, or one bad afternoon locks a client out
        forever."""
        old = time.time() - (rs._FAILURE_WINDOW + 1)
        rs._auth_failures["203.0.113.9"] = [old] * 10

        assert rs._is_rate_limited("203.0.113.9") is False

    def test_expired_entries_are_pruned_not_just_ignored(self):
        old = time.time() - (rs._FAILURE_WINDOW + 1)
        rs._auth_failures["203.0.113.9"] = [old] * 10

        rs._is_rate_limited("203.0.113.9")

        assert rs._auth_failures["203.0.113.9"] == []

    def test_limits_are_PER_IP(self):
        """One noisy client must not lock everyone else out."""
        for _ in range(5):
            rs._record_failure("203.0.113.9")
        assert rs._is_rate_limited("198.51.100.7") is False


class TestAdminMachines:
    def test_an_added_machine_is_an_admin(self):
        rs.add_admin_machine("UUID-1", "Simons Mac")
        assert rs.is_admin_machine_uuid("UUID-1") is True

    def test_an_unknown_machine_is_not(self):
        rs.add_admin_machine("UUID-1")
        assert rs.is_admin_machine_uuid("UUID-2") is False

    def test_an_EMPTY_uuid_is_never_an_admin(self):
        """Guards the admin websocket. A client that failed to identify
        itself must not fall through into admin authority -- and with no
        machines registered, `any()` over an empty list is already False, so
        the explicit empty check is what stops it once one exists."""
        rs.add_admin_machine("")
        assert rs.is_admin_machine_uuid("") is False

    def test_adding_twice_does_not_duplicate(self):
        rs.add_admin_machine("UUID-1", "first")
        rs.add_admin_machine("UUID-1", "second")
        assert len(rs.get_admin_machines()) == 1
        assert rs.get_admin_machines()[0]["label"] == "first"

    def test_removal_revokes_admin(self):
        rs.add_admin_machine("UUID-1")
        rs.remove_admin_machine("UUID-1")
        assert rs.is_admin_machine_uuid("UUID-1") is False

    def test_removal_persists(self, isolated):
        rs.add_admin_machine("UUID-1")
        rs.add_admin_machine("UUID-2")
        rs.remove_admin_machine("UUID-1")

        rs._admin_machines = []
        rs._load_admin_machines()

        assert [m["uuid"] for m in rs._admin_machines] == ["UUID-2"]

    def test_the_label_defaults_to_a_uuid_prefix(self):
        rs.add_admin_machine("ABCDEFGH-1234")
        assert rs.get_admin_machines()[0]["label"] == "ABCDEFGH"

    def test_get_admin_machines_returns_a_COPY(self):
        """The admin UI renders this list. Handing out the live one lets a
        caller grant admin by appending to it."""
        rs.add_admin_machine("UUID-1")

        rs.get_admin_machines().append({"uuid": "UUID-EVIL", "label": "x"})

        assert rs.is_admin_machine_uuid("UUID-EVIL") is False


class TestCorruptStateOnDisk:
    def test_unreadable_token_files_load_as_empty_rather_than_crashing(self, isolated):
        """These are read at server start. Raising means no remote server at
        all -- and it fails CLOSED: an unreadable allowed-list grants nobody,
        it does not grant everybody."""
        (isolated / "allowed_tokens.json").write_text("{not json")
        (isolated / "pending_registrations.json").write_text("{not json")
        (isolated / "revoked_tokens.json").write_text("{not json")

        rs._load_tokens()

        assert rs._allowed_tokens == {}
        assert rs._pending == {}
        assert rs._revoked_tokens == set()

    def test_an_unreadable_admin_file_grants_no_admin(self, isolated):
        (isolated / "admin_machines.json").write_text("{not json")

        rs._load_admin_machines()

        assert rs.get_admin_machines() == []
        assert rs.is_admin_machine_uuid("UUID-1") is False
