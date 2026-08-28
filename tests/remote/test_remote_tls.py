"""Self-signed TLS for the licence/admin channel.

The sibling of `sync/tls_util.py`, and it carries the same gap: the fingerprint
is generated, written and displayed, and the comment beside it says "so clients
can pin it" -- but nothing does. See docs/todo/bugs/014. The test below records
that rather than asserting a safety this channel does not have.

What IS load-bearing here:

  * ensure_cert() must not regenerate when a pair already exists. A new cert is
    a new fingerprint, which is the value the Remote Node screen shows.
  * The cert must cover the server IP in its SAN, or a client that ever starts
    verifying gets a name mismatch rather than a clean failure.
  * These files must stay distinct from the sync channel's, so rotating one
    cannot silently rotate the other.

Every path is redirected into tmp_path; nothing touches USER_DATA_DIR and no
socket is opened.
"""
from __future__ import annotations

import ipaddress
import ssl

import pytest

from backend.src.services.cluster.remote import tls


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    d = tmp_path / "remote"
    monkeypatch.setattr(tls, "_REMOTE_DIR", d)
    monkeypatch.setattr(tls, "_CERT_FILE", d / "server_cert.pem")
    monkeypatch.setattr(tls, "_KEY_FILE", d / "server_key.pem")
    monkeypatch.setattr(tls, "_FPRINT_FILE", d / "server_cert.fingerprint")
    return d


class TestEnsureCert:
    def test_it_generates_a_pair_and_a_fingerprint(self):
        cert, key = tls.ensure_cert()
        assert b"BEGIN CERTIFICATE" in cert.read_bytes()
        assert b"PRIVATE KEY" in key.read_bytes()
        assert tls.cert_fingerprint() != ""

    def test_it_does_not_regenerate_when_a_pair_exists(self):
        """A new cert is a new fingerprint -- the value the Remote Node screen
        tells the user to record."""
        cert, _ = tls.ensure_cert()
        first = cert.read_bytes()
        first_fp = tls.cert_fingerprint()

        tls.ensure_cert()

        assert cert.read_bytes() == first
        assert tls.cert_fingerprint() == first_fp

    def test_a_MISSING_KEY_forces_a_fresh_pair(self):
        """A cert without its key cannot serve anything. Returning the orphan
        pair would fail later inside load_cert_chain, at server start, with a
        less obvious error."""
        cert, key = tls.ensure_cert()
        first = cert.read_bytes()
        key.unlink()

        tls.ensure_cert()

        assert key.exists()
        assert cert.read_bytes() != first

    def test_it_creates_the_directory_itself(self, isolated):
        assert not isolated.exists()
        tls.ensure_cert()
        assert isolated.is_dir()


class TestTheCertificateItself:
    def _loaded(self):
        from cryptography import x509
        cert_path, _ = tls.ensure_cert()
        return x509.load_pem_x509_certificate(cert_path.read_bytes())

    def test_the_server_ip_is_in_the_san(self):
        """Not just the common name. A client that ever starts verifying
        matches against the SAN, and a missing entry there is a name mismatch
        rather than a clean, diagnosable failure."""
        from cryptography import x509
        san = self._loaded().extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value

        assert ipaddress.IPv4Address(tls.SERVER_HOST) in san.get_values_for_type(
            x509.IPAddress)

    def test_localhost_is_covered_too(self):
        """The admin UI connects to the server on the same box."""
        from cryptography import x509
        san = self._loaded().extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        assert "localhost" in san.get_values_for_type(x509.DNSName)

    def test_it_is_long_lived(self):
        """Ten years. An expiring cert on a headless VPS fails at a moment
        nobody is watching, and re-pairing needs the user present."""
        cert = self._loaded()
        span = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert span.days > 3000

    def test_it_is_self_signed(self):
        cert = self._loaded()
        assert cert.issuer == cert.subject


class TestFingerprint:
    def test_it_matches_the_certificate_on_disk(self):
        """If these diverge, the value the user is told to pin is not the one
        the server presents."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        cert_path, _ = tls.ensure_cert()
        loaded = x509.load_pem_x509_certificate(cert_path.read_bytes())

        assert tls.cert_fingerprint() == loaded.fingerprint(hashes.SHA256()).hex(":")

    def test_it_is_sha256(self):
        tls.ensure_cert()
        assert len(tls.cert_fingerprint().split(":")) == 32

    def test_it_is_empty_before_any_cert_exists(self):
        """Read by the Remote Node screen, which must render on a machine
        that has never started the admin server."""
        assert tls.cert_fingerprint() == ""


class TestSslContexts:
    def test_the_server_context_loads_the_generated_pair(self):
        assert isinstance(tls.server_ssl_context(), ssl.SSLContext)

    def test_the_server_context_generates_the_cert_if_needed(self, isolated):
        tls.server_ssl_context()
        assert (isolated / "server_cert.pem").exists()

    def test_the_client_context_does_not_verify_and_NOTHING_PINS_THE_CERT(self):
        """Records a real gap. Read docs/todo/bugs/014 before touching this.

        The comment beside the fingerprint write says it is stored "so clients
        can pin it". No client does. Nothing in the tree calls getpeercert(),
        and this module's fingerprint is only ever logged and displayed. The
        licence/admin channel is therefore encrypted but UNAUTHENTICATED.

        Asserted as it stands so the gap is visible in the suite rather than
        implied by a comment. It should change when 014 does."""
        ctx = tls.client_ssl_context()
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_the_client_context_is_a_client_context(self):
        """A server-side protocol here fails the handshake in a way that looks
        like a network fault rather than a configuration error."""
        assert tls.client_ssl_context().protocol == ssl.PROTOCOL_TLS_CLIENT
