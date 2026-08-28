"""Admin password hashing for the remote console.

This is the credential that gates admin authority over every connected client
-- licence issuance, remote update, the lot. It had no dedicated test.

The properties worth pinning are the ones whose absence is invisible from a
green run: that the stored file never contains the password, that two installs
with the same password produce different hashes, that verification is
constant-time, and above all that an absent or damaged hash file denies rather
than admits.

No password is ever written to the real USER_DATA_DIR -- the module's hash path
is redirected into tmp_path for every test.
"""
from __future__ import annotations

import hashlib

import pytest

from backend.src.services.cluster.remote import auth


@pytest.fixture(autouse=True)
def isolated_hash_file(monkeypatch, tmp_path):
    """Never touch the real admin hash. Every test gets its own."""
    monkeypatch.setattr(auth, "_HASH_FILE", tmp_path / "remote" / "admin_password.hash")
    return auth._HASH_FILE


class TestRoundTrip:
    def test_the_right_password_verifies(self):
        auth.set_password("correct horse battery staple")
        assert auth.verify_password("correct horse battery staple") is True

    def test_the_wrong_password_does_not(self):
        auth.set_password("correct horse battery staple")
        assert auth.verify_password("correct horse battery stapl") is False

    def test_verification_is_case_sensitive(self):
        auth.set_password("Secret")
        assert auth.verify_password("secret") is False

    def test_an_empty_attempt_does_not_pass(self):
        auth.set_password("Secret")
        assert auth.verify_password("") is False

    def test_setting_a_new_password_replaces_the_old_one(self):
        auth.set_password("first")
        auth.set_password("second")
        assert auth.verify_password("second") is True
        assert auth.verify_password("first") is False, "the old password still works"


class TestWhatIsStored:
    def test_the_password_is_not_in_the_file(self, isolated_hash_file):
        """The obvious catastrophe. Worth asserting rather than assuming."""
        auth.set_password("correct horse battery staple")
        contents = isolated_hash_file.read_text(encoding="utf-8")
        assert "correct horse battery staple" not in contents
        assert "correct" not in contents

    def test_it_is_salted_so_two_installs_differ(self):
        """Same password, two machines, two hashes. Without a per-install salt
        a leaked file could be compared across installs, and one cracked hash
        would be every install with that password."""
        auth.set_password("same password")
        first = auth._HASH_FILE.read_text(encoding="utf-8")
        auth.set_password("same password")
        second = auth._HASH_FILE.read_text(encoding="utf-8")
        assert first != second

    def test_the_stored_digest_is_scrypt_of_the_stored_salt(self):
        """Pins the KDF. Swapping scrypt for a plain digest would still round
        trip and still look salted, and would be much cheaper to attack."""
        auth.set_password("pw")
        salt_hex, digest_hex = auth._HASH_FILE.read_text(encoding="utf-8").split(":")
        expected = hashlib.scrypt(b"pw", salt=bytes.fromhex(salt_hex),
                                  n=2**14, r=8, p=1, dklen=32)
        assert bytes.fromhex(digest_hex) == expected


class TestItFailsClosed:
    """Every one of these denies access. A bug that makes any of them ADMIT is
    a remote admin bypass."""

    def test_no_hash_file_denies(self):
        assert auth.verify_password("anything") is False

    def test_no_hash_file_denies_even_an_empty_password(self):
        assert auth.verify_password("") is False

    def test_an_empty_file_denies(self, isolated_hash_file):
        isolated_hash_file.parent.mkdir(parents=True, exist_ok=True)
        isolated_hash_file.write_text("", encoding="utf-8")
        assert auth.verify_password("") is False
        assert auth.verify_password("anything") is False

    @pytest.mark.parametrize("junk", [
        "not-a-hash",                 # no separator
        "deadbeef",                   # salt only
        ":",                          # both halves empty
        "zz:zz",                      # not hex
        "abc:def:ghi",                # too many parts
    ])
    def test_a_damaged_file_denies_rather_than_raising(self, isolated_hash_file, junk):
        """A corrupted hash file must lock admin out, not crash the handler --
        and certainly not fall through to allowed."""
        isolated_hash_file.parent.mkdir(parents=True, exist_ok=True)
        isolated_hash_file.write_text(junk, encoding="utf-8")
        assert auth.verify_password("anything") is False


class TestPasswordIsSet:
    def test_false_before_anything_is_stored(self):
        assert auth.password_is_set() is False

    def test_true_once_stored(self):
        auth.set_password("pw")
        assert auth.password_is_set() is True

    def test_an_empty_file_does_not_count_as_set(self, isolated_hash_file):
        """Otherwise a truncated file would present as "configured" while
        verify_password refuses everything -- an install locked out with no
        prompt to set a password."""
        isolated_hash_file.parent.mkdir(parents=True, exist_ok=True)
        isolated_hash_file.write_text("", encoding="utf-8")
        assert auth.password_is_set() is False
