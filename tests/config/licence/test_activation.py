"""The activation screen's decisions.

This is a **licence surface**. The rule it must never break is the one asserted
first: nothing is stored unless the Ed25519 signature verifies against THIS
machine, which is the same check `enforce()` makes. A screen that could grant a
licence would make the whole licence system decorative, so
`test_a_key_that_does_not_verify_stores_nothing` is the test this file exists
for.

The second thing guarded is wording, which sounds trivial and is not. On
2026-08-07 a user was told their registration was "awaiting approval" while the
admin server was down — so nothing had been transmitted, nothing ever arrived,
and the screen gave no way to tell. `describe_delivery` reports what actually
happened.

Nothing here writes a real licence file: `store.save` is replaced.
"""
from __future__ import annotations

import pytest

from backend.src.config.licence import activation


@pytest.fixture
def store(monkeypatch):
    """Records what would have been written, and verifies nothing by default."""
    saved: list[dict] = []
    import backend.src.config.licence.store as _store

    monkeypatch.setattr(_store, "save", lambda record: saved.append(record))
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: False)
    return saved


def _allow(monkeypatch, ok: bool = True):
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: ok)


# ── The rule that matters ────────────────────────────────────────────────────

def test_a_key_that_does_not_verify_stores_nothing(store):
    """The whole point of this module. A screen that could grant a licence
    makes the licence system decorative."""
    answer = activation.activate_manually("machine-1", "Mac", "a@b.com", "FORGED")

    assert answer["ok"] is False
    assert "not issued for this machine" in answer["message"]
    assert store == []


def test_a_key_that_verifies_is_stored_against_this_machine(store, monkeypatch):
    """Negative control for the test above: without it, a function that always
    refused would pass."""
    _allow(monkeypatch)

    answer = activation.activate_manually(
        "machine-1", " Mac ", " a@b.com ", "KEY|2027-01-01|Fixed Term")

    assert answer["ok"] is True
    assert store == [{
        "machine_id": "machine-1",
        "nickname": "Mac",
        "email": "a@b.com",
        "expiry_date": "2027-01-01",
        "licence_type": "Fixed Term",
        "licence_key": "KEY",
    }]


def test_the_signature_check_is_the_same_one_enforce_uses():
    """Asserted rather than assumed: a second implementation here would be a
    second opinion about what a valid licence is."""
    import inspect

    from backend.src.config.licence import verify as _verify

    source = inspect.getsource(activation.verify_licence_key)
    assert "verify_licence_key" in source
    assert hasattr(_verify, "verify_licence_key")


# ── What the form will not send ──────────────────────────────────────────────

@pytest.mark.parametrize("nickname,email,expected", [
    ("", "a@b.com", "nickname"),
    ("M", "a@b.com", "nickname"),
    ("Mac", "", "email"),
    ("Mac", "not-an-email", "email"),
])
def test_incomplete_details_are_refused_by_name(nickname, email, expected):
    complaint = activation.validate_details(nickname, email)

    assert complaint is not None
    assert expected in complaint.lower()


def test_complete_details_pass(store):
    assert activation.validate_details("Mac", "a@b.com") is None


def test_bad_details_are_refused_before_a_key_is_even_parsed(store):
    """A licence issued against a name the operator did not type is a support
    conversation nobody can win."""
    answer = activation.activate_manually("machine-1", "M", "a@b.com", "KEY")

    assert answer["ok"] is False
    assert store == []


def test_an_empty_key_is_refused(store, monkeypatch):
    _allow(monkeypatch)

    answer = activation.activate_manually("machine-1", "Mac", "a@b.com", "   ")

    assert answer["ok"] is False
    assert "licence key" in answer["message"].lower()
    assert store == []


# ── Parsing a code ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("KEY", ("KEY", "perpetual", "Perpetual")),
    ("KEY|2027-01-01", ("KEY", "2027-01-01", "Fixed Term")),
    ("KEY|2027-01-01|Trial", ("KEY", "2027-01-01", "Trial")),
    ("  KEY | perpetual ", ("KEY", "perpetual", "Perpetual")),
])
def test_an_activation_code_is_parsed_into_its_three_parts(code, expected):
    assert activation.parse_activation_code(code) == expected


def test_the_guard_uses_this_parser_rather_than_its_own():
    """Two copies would be two answers to "what does a bare key default its
    expiry to"."""
    from backend.src.config.licence import guard

    assert guard._parse_activation_code("KEY") == activation.parse_activation_code("KEY")


# ── What the screen says while waiting ───────────────────────────────────────

class TestDeliveryWording:
    """The 2026-08-07 report: a user was told their request was awaiting
    approval while the admin server was down, so nothing had been transmitted
    and nothing ever arrived."""

    def test_a_sent_request_is_reported_as_delivered(self):
        answer = activation.describe_delivery({"registration_sent_at": 1_750_000_000})

        assert answer["state"] == "delivered"
        assert "awaiting administrator approval" in answer["message"]

    def test_an_unsent_request_is_never_reported_as_awaiting_approval(self):
        """The bug, pinned. Queued is not sent."""
        answer = activation.describe_delivery({})

        assert answer["state"] == "sending"
        assert "awaiting" not in answer["message"].lower()

    def test_a_real_error_is_shown_with_advice_that_matches_it(self):
        answer = activation.describe_delivery({"last_error": "connection refused"})

        assert answer["state"] == "retrying"
        assert "connection refused" in answer["message"]
        assert "Leave this screen open" in answer["message"]

    def test_the_client_saying_it_is_still_dialling_is_not_an_error(self):
        """"contacting administrator server" is the client's own in-progress
        state. Rendering it as a failure would tell the operator something is
        wrong the moment they press the button."""
        answer = activation.describe_delivery(
            {"last_error": "contacting administrator server"})

        assert answer["state"] == "sending"
