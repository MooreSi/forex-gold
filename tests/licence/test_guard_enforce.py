"""The startup licence gate.

`enforce()` runs before anything else and decides whether this install may
start. Every failure path must land on the ACTIVATION screen rather than a
dead-end error, and the reason is recoverability: the activation screen can
request a new licence and can accept one pushed by the admin console, so a
stranded install is fixable without physical access to the machine. A dead-end
error page is not -- the remote client never starts, so the admin cannot push a
corrected key either.

The subtle one is fingerprint drift. An OS update can change how hardware
values are reported, so the current fingerprint stops matching the stored one
even though the machine has not changed. The signature is therefore verified
against the STORED id -- the one the key was actually issued for -- and if it
passes, the drift is benign. Verifying against the new fingerprint instead
would always fail, since the key was never signed for it, which is confirmed to
have turned every benign drift into a false "invalid or tampered" error.

Nothing here weakens the gate. The error screen is replaced so it does not exit
the process or start a NiceGUI server; what is asserted is that each path
REACHES it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.src.config.licence import guard


@pytest.fixture
def gate(monkeypatch):
    """Everything enforce() touches, under control and recorded."""
    from backend.src.config.licence import store as _store

    state = {
        "data": {"licence_key": "KEY", "machine_id": "MACHINE-1",
                 "expiry_date": "perpetual"},
        "fingerprint": "MACHINE-1",
        "verifies": True,
        "shown": [],      # (reason, allow_register)
        "saved": [],
        "cleared": 0,
    }

    def _clear():
        state["cleared"] += 1

    monkeypatch.setattr(_store, "load", lambda: state["data"])
    monkeypatch.setattr(_store, "save", lambda d: state["saved"].append(d))
    monkeypatch.setattr(_store, "clear", _clear)
    monkeypatch.setattr(guard, "get_fingerprint", lambda: state["fingerprint"],
                        raising=False)
    monkeypatch.setattr(
        "backend.src.config.licence.fingerprint.get_fingerprint",
        lambda: state["fingerprint"])
    monkeypatch.setattr(guard, "_verify_licence_key",
                        lambda mid, exp, key: state["verifies"](mid, exp, key)
                        if callable(state["verifies"]) else state["verifies"])
    monkeypatch.setattr(guard, "_show_error_and_exit",
                        lambda reason, allow_register=False:
                        state["shown"].append((reason, allow_register)))
    return state


def _future(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


class TestAValidLicencePasses:
    def test_a_perpetual_licence_starts_the_app(self, gate):
        guard.enforce()
        assert gate["shown"] == []
        assert gate["cleared"] == 0

    def test_an_unexpired_dated_licence_starts_the_app(self, gate):
        gate["data"]["expiry_date"] = _future()
        guard.enforce()
        assert gate["shown"] == []

    def test_a_licence_expiring_TODAY_is_still_valid(self, gate):
        """The comparison is on dates, and > not >=. A licence is good for the
        whole of its final day."""
        gate["data"]["expiry_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        guard.enforce()
        assert gate["shown"] == []


class TestMissingOrIncompleteStore:
    @pytest.mark.parametrize("missing", ["licence_key", "machine_id", "expiry_date"])
    def test_any_missing_field_shows_activation(self, gate, missing):
        """A partial store is not a licence. Each field is load-bearing:
        without machine_id or expiry_date there is nothing to verify the
        signature against."""
        del gate["data"][missing]

        guard.enforce()

        assert len(gate["shown"]) == 1
        assert gate["shown"][0][1] is True, "registration was not offered"

    @pytest.mark.parametrize("empty", ["", None])
    def test_an_empty_field_counts_as_missing(self, gate, empty):
        gate["data"]["licence_key"] = empty
        guard.enforce()
        assert len(gate["shown"]) == 1

    def test_an_entirely_empty_store_shows_activation(self, gate):
        gate["data"] = {}
        guard.enforce()
        assert len(gate["shown"]) == 1
        assert gate["shown"][0][1] is True


class TestAnInvalidSignature:
    def test_it_CLEARS_THE_STORE_and_shows_activation(self, gate):
        """A key that no longer verifies is far more often stale than forged
        -- an upgrade brings a new verify.py and keys from a retired signing
        scheme stop validating. Clearing lets the activation screen accept a
        reissued one; a genuinely forged key still gets nowhere, because that
        screen only ever admits a key that verifies."""
        gate["verifies"] = False

        guard.enforce()

        assert gate["cleared"] == 1
        assert len(gate["shown"]) == 1
        assert gate["shown"][0][1] is True

    def test_it_does_NOT_fall_through_to_the_expiry_check(self, gate):
        """One screen, and enforce returns. Continuing past a failed signature
        would let an unsigned key reach the "licence OK" path."""
        gate["verifies"] = False
        gate["data"]["expiry_date"] = _past()

        guard.enforce()

        assert len(gate["shown"]) == 1


class TestFingerprintDrift:
    def test_a_drifted_fingerprint_with_a_GOOD_signature_is_accepted(self, gate):
        """The benign case. An OS update changed how the hardware reports
        itself; the key is still genuine for this machine."""
        gate["fingerprint"] = "MACHINE-1-AFTER-OS-UPDATE"

        guard.enforce()

        assert gate["shown"] == []
        assert gate["cleared"] == 0

    def test_the_store_is_UPDATED_to_the_new_fingerprint(self, gate):
        """So future startups skip this path entirely."""
        gate["fingerprint"] = "MACHINE-1-AFTER-OS-UPDATE"

        guard.enforce()

        assert gate["saved"][0]["machine_id"] == "MACHINE-1-AFTER-OS-UPDATE"
        assert gate["saved"][0]["licence_key"] == "KEY"

    def test_the_signature_is_checked_against_the_STORED_id(self, gate):
        """The whole point. The key was only ever signed for the original id,
        so checking it against the new fingerprint always fails -- which is
        confirmed to have turned every benign drift into a false 'invalid or
        tampered' error."""
        seen = []

        def _verify(mid, exp, key):
            seen.append(mid)
            return mid == "MACHINE-1"

        gate["verifies"] = _verify
        gate["fingerprint"] = "MACHINE-1-AFTER-OS-UPDATE"

        guard.enforce()

        assert seen == ["MACHINE-1"], f"verified against {seen}"
        assert gate["shown"] == []

    def test_it_is_NOT_RE_VERIFIED_against_the_new_id_afterwards(self, gate):
        """already_verified exists for this. A second check against the new
        fingerprint would fail on a key that was just proven genuine."""
        calls = []

        def _verify(mid, exp, key):
            calls.append(mid)
            return mid == "MACHINE-1"

        gate["verifies"] = _verify
        gate["fingerprint"] = "MACHINE-1-AFTER-OS-UPDATE"

        guard.enforce()

        assert calls == ["MACHINE-1"], "the key was re-verified against the new id"

    def test_a_DIFFERENT_MACHINE_is_still_refused(self, gate):
        """Drift tolerance must not become "any machine". A key that does not
        verify against the id it was issued for is not this machine's."""
        gate["fingerprint"] = "A-COMPLETELY-DIFFERENT-MACHINE"
        gate["verifies"] = False

        guard.enforce()

        assert gate["cleared"] == 1
        assert len(gate["shown"]) == 1
        assert "different machine" in gate["shown"][0][0].lower()
        assert gate["saved"] == [], "the store was updated for a foreign machine"


class TestExpiry:
    def test_an_expired_licence_shows_activation(self, gate):
        gate["data"]["expiry_date"] = _past()

        guard.enforce()

        assert len(gate["shown"]) == 1
        assert "expired" in gate["shown"][0][0].lower()
        assert gate["shown"][0][1] is True

    def test_an_expired_licence_is_NOT_cleared(self, gate):
        """Deliberate, and the opposite of the signature path: an expired key
        is genuine. Re-saving is the activation screen's job once a renewal
        actually arrives."""
        gate["data"]["expiry_date"] = _past()

        guard.enforce()

        assert gate["cleared"] == 0

    def test_perpetual_skips_the_expiry_check_entirely(self, gate):
        gate["data"]["expiry_date"] = "perpetual"
        guard.enforce()
        assert gate["shown"] == []

    def test_an_UNRECOGNISED_expiry_format_is_treated_as_VALID(self, gate):
        """Records real behaviour, and it is the permissive direction: a date
        this build cannot parse lets the app start rather than locking a
        paying user out over a format change. Pinned so the choice is visible
        -- it is a licensing decision, not an accident, and anyone tightening
        it should know it is load-bearing for exactly that reason."""
        gate["data"]["expiry_date"] = "31/12/2027"

        guard.enforce()

        assert gate["shown"] == []


class TestParsingAnActivationCode:
    def test_a_bare_key_is_perpetual(self):
        key, expiry, ltype = guard._parse_activation_code("ABC123")
        assert (key, expiry, ltype) == ("ABC123", "perpetual", "Perpetual")

    def test_a_dated_key_defaults_to_fixed_term(self):
        """The type is derived, not assumed. A dated key labelled Perpetual
        would display as never-expiring while still expiring."""
        key, expiry, ltype = guard._parse_activation_code("ABC123|2027-01-01")
        assert (key, expiry, ltype) == ("ABC123", "2027-01-01", "Fixed Term")

    def test_an_explicit_type_wins(self):
        _k, _e, ltype = guard._parse_activation_code("ABC123|2027-01-01|Trial")
        assert ltype == "Trial"

    def test_whitespace_is_stripped_from_every_part(self):
        """These are pasted from an email. A trailing space in the key makes
        the signature fail with no clue why."""
        key, expiry, ltype = guard._parse_activation_code("  ABC123 | 2027-01-01 | Trial  ")
        assert (key, expiry, ltype) == ("ABC123", "2027-01-01", "Trial")

    def test_extra_fields_are_ignored_rather_than_breaking(self):
        key, expiry, ltype = guard._parse_activation_code("ABC|2027-01-01|Trial|extra")
        assert (key, expiry, ltype) == ("ABC", "2027-01-01", "Trial")
