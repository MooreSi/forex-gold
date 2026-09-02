"""The certificate authority the build ships, and the key it must never ship.

bugs/014 closed 2026-09-02. `ca_cert.pem` sits beside `ca.py` so every build
carries the authority, and `is_ca_verified` turns on for the internet path the
moment it is present.

The dangerous half is what is NOT here. `ca_key.pem` signs certificates this
app trusts without question: anyone holding it can mint one and the client will
accept it, which is a strictly worse position than the unauthenticated channel
this replaced -- that at least required being on the network path. The key
lives offline, outside the repository, and `test_the_private_key_is_not_in_the_repo`
is the thing standing between a convenient `cp` and a total compromise.
"""
from __future__ import annotations

import datetime
import pathlib
import subprocess

import pytest

from backend.src.services.cluster.remote import ca, tls

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestThePrivateKeyIsNowhereNearThisRepository:
    def test_the_private_key_is_not_in_the_repo(self):
        """The one that matters. A CA key in a repo -- private or not -- is a
        signing oracle for everyone who can read it."""
        found = [str(p.relative_to(REPO))
                 for p in REPO.rglob("ca_key.pem")
                 if ".venv" not in p.parts and ".git" not in p.parts]

        assert found == [], f"the CA private key is in the working tree: {found}"

    def test_it_is_not_tracked_by_git(self):
        out = subprocess.run(["git", "ls-files", "--", "*ca_key.pem"],
                             cwd=REPO, capture_output=True, text=True).stdout

        assert out.strip() == "", f"the CA private key is tracked: {out!r}"

    def test_gitignore_would_stop_it_being_added(self):
        """Belt and braces: the two tests above are point-in-time, this one
        stops the mistake being possible in the first place."""
        out = subprocess.run(
            ["git", "check-ignore", "-q",
             "backend/src/services/cluster/remote/ca_key.pem"],
            cwd=REPO, capture_output=True, text=True)

        assert out.returncode == 0, "ca_key.pem is NOT gitignored"

    def test_no_private_key_material_is_bundled(self):
        """A cert file that somehow contained a key would defeat every check
        above by not being called ca_key.pem."""
        text = ca.BUNDLED_CA.read_text(encoding="utf-8")

        assert "PRIVATE KEY" not in text
        assert text.lstrip().startswith("-----BEGIN CERTIFICATE-----")


class TestTheBundledAuthority:
    def test_the_build_ships_one(self):
        assert ca.bundled_ca_path() is not None
        assert ca.bundled_ca_path().exists()

    def test_it_is_actually_a_certificate_authority(self):
        """A leaf certificate here would verify nothing and refuse every
        connection -- the lockout the staged rollout exists to avoid."""
        from cryptography import x509

        cert = ca.load_cert(ca.BUNDLED_CA)
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints)

        assert basic.value.ca is True

    def test_it_has_not_expired(self):
        """When this authority expires, every client refuses the internet path
        at once. Ten years from 2026-09-02, and this test is the reminder."""
        cert = ca.load_cert(ca.BUNDLED_CA)
        remaining = cert.not_valid_after_utc - datetime.datetime.now(
            datetime.timezone.utc)

        assert remaining.days > 30, f"the bundled CA expires in {remaining.days} days"

    def test_a_build_with_no_authority_does_NOT_claim_verification(self,
                                                                    monkeypatch):
        """Every build made before the cutover ships no `ca_cert.pem`. Such a
        build must fall back to the application-layer check, not announce that
        TLS established the peer -- which would skip `verify_or_pin` and send
        the licence token to anything that answered.

        Only mutation testing surfaces this one: with an authority present the
        guard looks redundant, and dropping it changes nothing until the day
        an older build runs the code.
        """
        monkeypatch.setattr(ca, "bundled_ca_path", lambda: None)

        assert tls.is_ca_verified(tls.SERVER_HOST) is False

        ok, why = tls.peer_is_acceptable(tls.SERVER_HOST, "some-fingerprint")
        assert "authority" not in why

    def test_the_internet_path_verifies_and_the_LAN_path_does_not(self):
        """Both halves of the owner's decision, in one place."""
        assert tls.is_ca_verified(tls.SERVER_HOST) is True
        assert tls.is_ca_verified("192.168.0.42") is False
        assert tls.is_ca_verified("127.0.0.1") is False
