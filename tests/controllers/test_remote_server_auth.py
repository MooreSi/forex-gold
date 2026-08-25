"""The remote server's token authority — who may connect, and who may not.

`backend/src/controllers/remote/` had **zero tests**: 2,116 lines deciding
licence-token issuance, revocation, and which machines hold admin rights.
That gap is also what blocked splitting the file, since a split you cannot
verify is a coin flip on the auth path.

This covers the decisions, not the transport. The websocket handler, TLS and
the LAN beacon are I/O; what matters here is the logic that answers "is this
token allowed", "does a revocation survive a restart", and "is this IP
locked out" — because every one of those failing open is a stranger on the
account.

Isolation: the module keeps its token stores as module-level dicts and writes
them to USER_DATA_DIR/remote/. Every test here redirects those paths to a
tmp_path and resets the dicts, so no test can read or corrupt a real
installation's tokens.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.src.services.cluster.remote import server


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the token files at a temp dir and clear the in-memory state.

    Both halves are required. The paths alone would leave a previous test's
    tokens in the module dicts; clearing the dicts alone would write to the
    developer's real remote/ directory.
    """
    remote_dir = tmp_path / "remote"
    monkeypatch.setattr(server, "_REMOTE_DIR", remote_dir)
    monkeypatch.setattr(server, "_TOKENS_FILE", remote_dir / "allowed_tokens.json")
    monkeypatch.setattr(server, "_PENDING_FILE", remote_dir / "pending_registrations.json")
    monkeypatch.setattr(server, "_REVOKED_FILE", remote_dir / "revoked_tokens.json")
    monkeypatch.setattr(server, "_ADMIN_MACHINES_FILE", remote_dir / "admin_machines.json")

    monkeypatch.setattr(server, "_allowed_tokens", {})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_revoked_tokens", set())
    monkeypatch.setattr(server, "_admin_machines", [])
    monkeypatch.setattr(server, "_connected", {})
    monkeypatch.setattr(server, "_auth_failures", {})
    # A stand-in signer. Since upstream 7251656 the server does not sign
    # licences itself -- the Ed25519 private key lives in KeyGen, outside this
    # repo, and forex_admin.py injects a signer through register_kg_callbacks.
    # Before that change this module imported keygen.generate_licence_key
    # directly, which is exactly the shared-secret hole the change closed. So a
    # test that expects an approved client to receive a key has to supply the
    # signer, the same way the admin does.
    monkeypatch.setattr(
        server, "_kg_sign_fn",
        lambda machine_id, expiry_date: f"SIG-{machine_id}-{expiry_date}",
    )
    return remote_dir


def _add_pending(token="tok-1", machine_id="MACHINE-A", ip="10.0.0.9"):
    server._pending[token] = {
        "hostname": "brothers-mac", "email": "b@example.com",
        "nickname": "Brother", "platform": "darwin",
        "machine_id": machine_id, "ip": ip,
    }
    return token


# ── Approval ─────────────────────────────────────────────────────────────

def test_approving_a_pending_registration_moves_it_to_allowed(store):
    token = _add_pending()
    assert server.approve_registration(token, "Brother's Mac") is True

    assert token in server._allowed_tokens
    assert token not in server._pending
    entry = server._allowed_tokens[token]
    assert entry["name"] == "Brother's Mac"
    assert entry["machine_id"] == "MACHINE-A"
    assert entry["licence_key"], "an approved client must get a licence key"


def test_approving_an_unknown_token_is_refused(store):
    """The failure that matters: approving something never registered would
    mint a licence for a machine nobody vetted."""
    assert server.approve_registration("never-seen", "Someone") is False
    assert server._allowed_tokens == {}


@pytest.mark.parametrize("subscription,expect_perpetual", [
    ("Perpetual", True),
    ("6 Months", False),
    ("1 Year", False),
    ("2 Years", False),
    ("3 Years", False),
])
def test_expiry_follows_the_subscription_type(store, subscription, expect_perpetual):
    token = _add_pending()
    server.approve_registration(token, "X", subscription_type=subscription)
    expiry = server._allowed_tokens[token]["expiry_date"]
    if expect_perpetual:
        assert expiry == "perpetual"
    else:
        assert expiry != "perpetual" and len(expiry) == 10   # YYYY-MM-DD


def test_an_unrecognised_subscription_type_does_not_silently_grant_forever(store):
    """Defensive: a typo'd subscription must not be more generous than any
    real one. Today it maps to perpetual -- pinned so a change is deliberate."""
    token = _add_pending()
    server.approve_registration(token, "X", subscription_type="Lifetime Platinum")
    assert server._allowed_tokens[token]["expiry_date"] == "perpetual"


def test_approval_without_a_machine_id_issues_no_licence_key(store):
    """A licence key is an HMAC over the machine id. No machine, no key --
    it must not fall back to a key that would validate anywhere."""
    token = _add_pending(machine_id="")
    server.approve_registration(token, "X")
    assert server._allowed_tokens[token]["licence_key"] == ""


def test_approval_clears_the_ip_rate_limit(store):
    """A client that failed auth while pending would otherwise be locked out
    of the connection that delivers its licence."""
    token = _add_pending(ip="10.0.0.9")
    for _ in range(server._MAX_FAILURES):
        server._record_failure("10.0.0.9")
    assert server._is_rate_limited("10.0.0.9") is True

    server.approve_registration(token, "X")
    assert server._is_rate_limited("10.0.0.9") is False


# ── Revocation ───────────────────────────────────────────────────────────

def test_revoking_removes_the_token_and_remembers_it(store):
    token = _add_pending()
    server.approve_registration(token, "X")

    server.revoke_token(token)

    assert token not in server._allowed_tokens
    assert token in server._revoked_tokens


def test_revoking_also_clears_a_pending_re_registration(store):
    """Otherwise a revoked client re-registers and sits in the approval
    queue looking like a fresh request."""
    token = _add_pending()
    server.approve_registration(token, "X")
    _add_pending(token)                      # client re-registers
    server.revoke_token(token)
    assert token not in server._pending


def test_a_revocation_survives_a_restart(store):
    """The whole point of the revoked list. Reloading from disk must not
    hand a revoked token back its access."""
    token = _add_pending()
    server.approve_registration(token, "X")
    server.revoke_token(token)

    server._allowed_tokens, server._pending, server._revoked_tokens = {}, {}, set()
    server._load_tokens()

    assert token in server._revoked_tokens
    assert token not in server._allowed_tokens


def test_re_approving_lifts_a_prior_revocation(store):
    """Deliberate behaviour: an owner re-approving a client must actually
    restore access, not leave it silently blocked by the revoke list."""
    token = _add_pending()
    server.approve_registration(token, "X")
    server.revoke_token(token)
    assert token in server._revoked_tokens

    _add_pending(token)
    server.approve_registration(token, "X again")

    assert token in server._allowed_tokens
    assert token not in server._revoked_tokens


# ── Persistence ──────────────────────────────────────────────────────────

def test_tokens_round_trip_through_disk(store):
    token = _add_pending()
    server.approve_registration(token, "Brother's Mac")

    server._allowed_tokens, server._pending, server._revoked_tokens = {}, {}, set()
    server._load_tokens()

    assert server._allowed_tokens[token]["name"] == "Brother's Mac"


def test_a_corrupt_token_file_fails_closed_not_crashed(store):
    """A truncated write must not take the server down on boot -- but it also
    must not resurrect access. Empty means nobody is allowed, which is the
    safe direction."""
    store.mkdir(parents=True, exist_ok=True)
    server._TOKENS_FILE.write_text("{ this is not json")
    server._REVOKED_FILE.write_text("]]]")

    server._load_tokens()

    assert server._allowed_tokens == {}
    assert server._revoked_tokens == set()


def test_load_tokens_on_a_fresh_install_starts_empty(store):
    server._load_tokens()
    assert server._allowed_tokens == {}
    assert server._pending == {}
    assert server._revoked_tokens == set()


# ── Rate limiting ────────────────────────────────────────────────────────

def test_an_ip_is_not_limited_until_the_threshold(store):
    for _ in range(server._MAX_FAILURES - 1):
        server._record_failure("1.2.3.4")
    assert server._is_rate_limited("1.2.3.4") is False


def test_an_ip_is_limited_at_the_threshold(store):
    for _ in range(server._MAX_FAILURES):
        server._record_failure("1.2.3.4")
    assert server._is_rate_limited("1.2.3.4") is True


def test_failures_age_out_of_the_window(store):
    """Otherwise one bad afternoon locks a legitimate client out forever."""
    stale = time.time() - server._FAILURE_WINDOW - 1
    server._auth_failures["1.2.3.4"] = [stale] * server._MAX_FAILURES
    assert server._is_rate_limited("1.2.3.4") is False


def test_rate_limiting_is_per_ip(store):
    for _ in range(server._MAX_FAILURES):
        server._record_failure("1.2.3.4")
    assert server._is_rate_limited("1.2.3.4") is True
    assert server._is_rate_limited("5.6.7.8") is False


# ── Admin machines ───────────────────────────────────────────────────────

def test_adding_an_admin_machine_grants_it(store):
    server.add_admin_machine("UUID-1", "Main Mac")
    assert server.is_admin_machine_uuid("UUID-1") is True
    assert [m["uuid"] for m in server.get_admin_machines()] == ["UUID-1"]


def test_an_unknown_uuid_is_not_an_admin(store):
    server.add_admin_machine("UUID-1", "Main Mac")
    assert server.is_admin_machine_uuid("UUID-2") is False
    assert server.is_admin_machine_uuid("") is False


def test_removing_an_admin_machine_revokes_it(store):
    server.add_admin_machine("UUID-1", "Main Mac")
    server.remove_admin_machine("UUID-1")
    assert server.is_admin_machine_uuid("UUID-1") is False


def test_adding_the_same_machine_twice_does_not_duplicate_it(store):
    server.add_admin_machine("UUID-1", "Main Mac")
    server.add_admin_machine("UUID-1", "Main Mac Renamed")
    assert len(server.get_admin_machines()) == 1


def test_admin_machines_round_trip_through_disk(store):
    server.add_admin_machine("UUID-1", "Main Mac")
    server._admin_machines = []
    server._load_admin_machines()
    assert server.is_admin_machine_uuid("UUID-1") is True


# ── Read models ──────────────────────────────────────────────────────────

def test_get_all_clients_reports_approved_clients(store):
    token = _add_pending()
    server.approve_registration(token, "Brother's Mac")
    clients = server.get_all_clients()
    assert [c["name"] for c in clients] == ["Brother's Mac"]


def test_get_pending_registrations_reports_only_unapproved(store):
    approved = _add_pending("tok-approved")
    server.approve_registration(approved, "Approved")
    _add_pending("tok-waiting")

    pending = server.get_pending_registrations()
    tokens = [p.get("token") for p in pending]
    assert "tok-waiting" in tokens
    assert "tok-approved" not in tokens


# ── Negative control ─────────────────────────────────────────────────────

def test_the_fixture_really_isolates_the_store(store, tmp_path):
    """If this leaks, every test above is writing to a real installation's
    token files -- which would be worse than having no tests."""
    assert str(tmp_path) in str(server._TOKENS_FILE)
    token = _add_pending()
    server.approve_registration(token, "X")
    assert server._TOKENS_FILE.exists()
    written = json.loads(server._TOKENS_FILE.read_text(encoding="utf-8"))
    assert token in written
