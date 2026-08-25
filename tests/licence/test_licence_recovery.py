"""Every licence failure must land on the activation screen, not a dead end.

Before 2026-08-07, guard.enforce() showed an unrecoverable "invalid or has
been tampered with" error page whenever a stored key failed to verify. That
is the exact state every existing install lands in after the HMAC -> Ed25519
signing migration: the old key is genuine but no longer verifies against the
new verify.py. The error page never starts the remote client, so the admin
console could not push a corrected key either -- the install was unfixable
without physically sitting at the machine.

These tests pin the recovery route: each failure path clears (or keeps) the
store as appropriate and routes to the registration screen.
"""
import pytest

from backend.src.config.licence import guard


class _Routed(Exception):
    """Raised by the stub screen so enforce() stops where the real one would."""

    def __init__(self, reason, allow_register):
        self.reason = reason
        self.allow_register = allow_register
        super().__init__(reason)


@pytest.fixture
def routes(monkeypatch):
    """Capture where enforce() sends the user instead of rendering a UI."""
    def _stub(reason, allow_register=False):
        raise _Routed(reason, allow_register)

    monkeypatch.setattr(guard, "_show_error_and_exit", _stub)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Point the licence store at a temp file so the real one is untouched."""
    from backend.src.config.licence import store as _store

    monkeypatch.setattr(_store, "STORE_PATH", tmp_path / ".forex_trader_licence")
    return _store


def _set_fingerprint(monkeypatch, value):
    from backend.src.config.licence import fingerprint

    monkeypatch.setattr(fingerprint, "get_fingerprint", lambda: value)


def _accept_nothing(monkeypatch):
    """Make every signature check fail — a key from a retired signing scheme."""
    monkeypatch.setattr(guard, "_verify_licence_key", lambda *a, **k: False)


def _accept_everything(monkeypatch):
    monkeypatch.setattr(guard, "_verify_licence_key", lambda *a, **k: True)


def test_unverifiable_key_routes_to_registration_and_clears_store(
    routes, store, monkeypatch
):
    _set_fingerprint(monkeypatch, "MACHINE-A")
    _accept_nothing(monkeypatch)
    store.save({
        "machine_id":  "MACHINE-A",
        "expiry_date": "perpetual",
        "licence_key": "DEADBEEF" * 16,
    })

    with pytest.raises(_Routed) as routed:
        guard.enforce()

    assert routed.value.allow_register is True, (
        "a key that no longer verifies must offer re-registration, not a dead end"
    )
    assert routed.value.reason, "the user must be told why they are re-registering"
    assert store.load() is None, "the unusable key must not be left on disk"


def test_expired_licence_routes_to_registration_and_keeps_key(
    routes, store, monkeypatch
):
    _set_fingerprint(monkeypatch, "MACHINE-A")
    _accept_everything(monkeypatch)
    store.save({
        "machine_id":  "MACHINE-A",
        "expiry_date": "2020-01-01",
        "licence_key": "AA" * 64,
    })

    with pytest.raises(_Routed) as routed:
        guard.enforce()

    assert routed.value.allow_register is True
    assert "2020-01-01" in routed.value.reason
    assert store.load()["licence_key"] == "AA" * 64, (
        "an expired key is genuine — only a renewal should replace it"
    )


def test_key_for_another_machine_routes_to_registration(routes, store, monkeypatch):
    _set_fingerprint(monkeypatch, "MACHINE-B")
    _accept_nothing(monkeypatch)
    store.save({
        "machine_id":  "MACHINE-A",
        "expiry_date": "perpetual",
        "licence_key": "AA" * 64,
    })

    with pytest.raises(_Routed) as routed:
        guard.enforce()

    assert routed.value.allow_register is True
    assert store.load() is None


def test_fingerprint_drift_still_self_heals_without_re_registration(
    routes, store, monkeypatch
):
    """An OS update can change the fingerprint on an otherwise valid install.
    That path must keep working silently — it must not be swallowed by the new
    re-registration routes."""
    _set_fingerprint(monkeypatch, "MACHINE-A-DRIFTED")
    verified_against = []

    def _verify(machine_id, expiry_date, licence_key):
        verified_against.append(machine_id)
        return machine_id == "MACHINE-A"

    monkeypatch.setattr(guard, "_verify_licence_key", _verify)
    store.save({
        "machine_id":  "MACHINE-A",
        "expiry_date": "perpetual",
        "licence_key": "AA" * 64,
    })

    guard.enforce()  # no _Routed raised — the app is allowed to start

    assert store.load()["machine_id"] == "MACHINE-A-DRIFTED", (
        "the drifted fingerprint should be adopted so later startups skip this path"
    )
    assert verified_against == ["MACHINE-A"], (
        "re-verifying against the drifted id would always fail — the key was "
        "only ever signed for the original id"
    )


def test_valid_perpetual_licence_starts_the_app(routes, store, monkeypatch):
    _set_fingerprint(monkeypatch, "MACHINE-A")
    _accept_everything(monkeypatch)
    store.save({
        "machine_id":  "MACHINE-A",
        "expiry_date": "perpetual",
        "licence_key": "AA" * 64,
    })

    guard.enforce()  # no _Routed raised


def test_no_licence_at_all_routes_to_registration(routes, store, monkeypatch):
    _set_fingerprint(monkeypatch, "MACHINE-A")
    _accept_everything(monkeypatch)

    with pytest.raises(_Routed) as routed:
        guard.enforce()

    assert routed.value.allow_register is True
