"""Self-signed TLS for the Local/Remote sync channel.

The Mac talks to the VPS over TLS with no CA and no hostname check. That is a
deliberate choice for a fixed-IP box with no real certificate, and it is safe
for exactly one reason: the client pins the server's SHA-256 fingerprint. Take
the pinning away and this becomes an unauthenticated channel that merely looks
encrypted.

Two things therefore matter more than anything else here, and both are easy to
break without noticing:

  * client_ssl_context() has verify_mode CERT_NONE and check_hostname False.
    A test asserts that ON PURPOSE, so anyone "fixing" it has to read why.
  * ensure_cert() must NOT regenerate when a cert already exists. A new cert
    is a new fingerprint, and the Mac is pinning the old one -- silently
    regenerating breaks the link the moment the VPS restarts.

Every path is redirected into tmp_path; nothing touches USER_DATA_DIR and no
socket is opened.
"""
from __future__ import annotations

import ssl

import pytest

from backend.src.services.cluster.sync import tls_util


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    d = tmp_path / "sync"
    monkeypatch.setattr(tls_util, "_SYNC_DIR", d)
    monkeypatch.setattr(tls_util, "_CERT_FILE", d / "sync_cert.pem")
    monkeypatch.setattr(tls_util, "_KEY_FILE", d / "sync_key.pem")
    monkeypatch.setattr(tls_util, "_FPRINT_FILE", d / "sync_cert.fingerprint")
    return d


class TestEnsureCert:
    def test_it_generates_a_cert_key_and_fingerprint(self):
        cert, key = tls_util.ensure_cert("203.0.113.10")
        assert cert.exists() and key.exists()
        assert tls_util._FPRINT_FILE.exists()
        assert b"BEGIN CERTIFICATE" in cert.read_bytes()
        assert b"PRIVATE KEY" in key.read_bytes()

    def test_it_does_not_regenerate_when_one_already_exists(self):
        """THE load-bearing property. The Mac pins this fingerprint; a fresh
        cert on every call (or every VPS restart) would break the sync link
        with a mismatch the user has no obvious way to diagnose."""
        cert, key = tls_util.ensure_cert("203.0.113.10")
        first_cert = cert.read_bytes()
        first_fp = tls_util.cert_fingerprint()

        tls_util.ensure_cert("203.0.113.10")

        assert cert.read_bytes() == first_cert, "the certificate was regenerated"
        assert tls_util.cert_fingerprint() == first_fp, "the pinned fingerprint changed"

    def test_a_different_host_does_not_regenerate_either(self):
        """Same reasoning: the pin follows the cert, not the host argument."""
        tls_util.ensure_cert("203.0.113.10")
        first = tls_util.cert_fingerprint()
        tls_util.ensure_cert("198.51.100.7")
        assert tls_util.cert_fingerprint() == first

    def test_it_accepts_a_hostname_as_well_as_an_ip(self):
        """host is whatever the user typed in Settings > Remote Node. An IP
        goes in the SAN as an IPAddress, anything else as a DNSName -- passing
        a hostname to ip_address() raises, and that path must be handled."""
        cert, _ = tls_util.ensure_cert("vps.example.com")
        assert cert.exists()


class TestFingerprint:
    def test_it_matches_the_certificate_actually_written(self):
        """If these ever diverge, the value the user is told to pin is not the
        value the server will present, and sync can never connect."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        cert_path, _ = tls_util.ensure_cert("203.0.113.10")
        loaded = x509.load_pem_x509_certificate(cert_path.read_bytes())
        expected = loaded.fingerprint(hashes.SHA256()).hex(":")

        assert tls_util.cert_fingerprint() == expected

    def test_it_is_sha256_not_something_weaker(self):
        """32 bytes as colon-separated hex. SHA-1 would be 20."""
        tls_util.ensure_cert("203.0.113.10")
        fp = tls_util.cert_fingerprint()
        assert len(fp.split(":")) == 32, fp

    def test_it_is_empty_before_any_cert_exists(self):
        """The Remote Node screen reads this to show the value to pin. Raising
        here would break the screen on a machine that has never synced."""
        assert tls_util.cert_fingerprint() == ""


class TestSslContexts:
    def test_the_server_context_loads_the_generated_pair(self):
        ctx = tls_util.server_ssl_context("203.0.113.10")
        assert isinstance(ctx, ssl.SSLContext)

    def test_the_client_context_deliberately_does_not_verify(self):
        """DELIBERATE, and load-bearing. There is no CA and no DNS name for a
        bare VPS IP, so the client cannot do a normal check. Authentication
        comes from the caller comparing the presented cert's SHA-256 against
        the pinned value -- see client.py's _verify_fingerprint.

        Turning verification back on here without removing the pinning would
        not make this safer, it would just stop the link working. Removing the
        pinning while leaving this off would make it unauthenticated.
        """
        ctx = tls_util.client_ssl_context()
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_the_client_context_is_a_client_context(self):
        ctx = tls_util.client_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        # A server-side protocol here would fail the handshake in a way that
        # looks like a network fault rather than a configuration error.
        assert ctx.protocol == ssl.PROTOCOL_TLS_CLIENT


def test_the_sync_channel_keeps_its_own_cert_separate_from_remote():
    """sync/ and remote/ deliberately use different files so the licence
    channel and the node-sync channel can never interfere. Same-path would
    mean rotating one silently rotates the other."""
    from backend.src.services.cluster.remote import tls as remote_tls

    sync_names = {tls_util._CERT_FILE.name, tls_util._KEY_FILE.name}
    remote_names = {p.name for p in vars(remote_tls).values()
                    if hasattr(p, "name") and str(p).endswith((".pem", ".fingerprint"))}
    assert not (sync_names & remote_names), (
        f"sync and remote share certificate filenames: {sync_names & remote_names}")
