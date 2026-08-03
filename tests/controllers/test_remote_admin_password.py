"""Admin password hashing and verification.

This is the credential guarding the elevated admin connection to the remote
server — the one that can approve registrations, mint licences and revoke
clients. It had no tests.

The properties worth asserting are the ones that make a password store safe
rather than merely functional: the plaintext never lands on disk, two
identical passwords produce different stored hashes (salting), a wrong
password is rejected, and every corruption path fails **closed**.

That last one is the point. A verifier that returns True on a malformed hash
file is not a lock.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.src.controllers.remote import auth


@pytest.fixture
def hash_file(tmp_path, monkeypatch):
    """Redirect the hash file so no test can read or clobber a real one."""
    path = tmp_path / "remote" / "admin_password.hash"
    monkeypatch.setattr(auth, "_HASH_FILE", path)
    return path


# ── The basic contract ───────────────────────────────────────────────────

def test_a_set_password_verifies(hash_file):
    auth.set_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple") is True


def test_a_wrong_password_is_rejected(hash_file):
    auth.set_password("correct horse battery staple")
    assert auth.verify_password("Correct Horse Battery Staple") is False
    assert auth.verify_password("") is False
    assert auth.verify_password("correct horse battery stapl") is False


def test_password_is_set_reports_the_truth(hash_file):
    assert auth.password_is_set() is False
    auth.set_password("hunter2")
    assert auth.password_is_set() is True


def test_setting_a_new_password_replaces_the_old_one(hash_file):
    auth.set_password("first")
    auth.set_password("second")
    assert auth.verify_password("second") is True
    assert auth.verify_password("first") is False


def test_the_directory_is_created_if_missing(hash_file):
    assert not hash_file.parent.exists()
    auth.set_password("hunter2")
    assert hash_file.exists()


# ── The properties that make it a password store ─────────────────────────

def test_the_plaintext_never_touches_disk(hash_file):
    auth.set_password("correct horse battery staple")
    written = hash_file.read_text()
    assert "correct horse battery staple" not in written
    assert "correct" not in written


def test_the_same_password_hashes_differently_each_time(hash_file, tmp_path):
    """Salting. Without it, identical passwords produce identical hashes and
    a stolen file tells an attacker which machines share a password."""
    auth.set_password("same password")
    first = hash_file.read_text()

    auth.set_password("same password")
    second = hash_file.read_text()

    assert first != second
    assert auth.verify_password("same password") is True


def test_the_stored_format_is_salt_and_digest(hash_file):
    auth.set_password("hunter2")
    salt_hex, digest_hex = hash_file.read_text().split(":")
    assert len(bytes.fromhex(salt_hex)) == 32
    assert len(bytes.fromhex(digest_hex)) == 32


# ── Failing closed ───────────────────────────────────────────────────────

def test_verification_fails_when_no_password_is_set(hash_file):
    """Open by default would mean a fresh install has an admin account with
    no password on it."""
    assert auth.verify_password("anything") is False
    assert auth.verify_password("") is False


@pytest.mark.parametrize("corrupt", [
    "",                       # truncated to nothing
    "not-a-hash",             # no separator
    "zzz:zzz",                # not hex
    "abcd:",                  # missing digest
    ":abcd",                  # missing salt
    "a:b:c",                  # too many parts
])
def test_a_corrupt_hash_file_fails_closed(hash_file, corrupt):
    """Every malformed state must reject, never accept. A verifier that
    returns True on a broken file is not a lock."""
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_text(corrupt)
    assert auth.verify_password("anything") is False
    assert auth.verify_password("") is False


def test_an_empty_hash_file_does_not_count_as_configured(hash_file):
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_text("")
    assert auth.password_is_set() is False


# ── Negative control ─────────────────────────────────────────────────────

def test_the_fixture_isolates_the_hash_file(hash_file, tmp_path):
    """If this leaks, these tests overwrite the developer's real admin
    password."""
    assert str(tmp_path) in str(auth._HASH_FILE)
    auth.set_password("x")
    assert auth._HASH_FILE.exists()
    assert Path(auth._HASH_FILE).is_relative_to(tmp_path)
