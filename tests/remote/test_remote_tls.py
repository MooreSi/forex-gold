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

    def test_the_internet_path_is_now_CA_VERIFIED(self):
        """bugs/014, closed 2026-09-02.

        This test previously asserted the opposite, deliberately: it recorded
        the gap in the suite rather than leaving it implied by a comment, and
        said in its own docstring that it should change when 014 did. It has.

        The build now ships `ca_cert.pem`, so the handshake to the public
        address establishes who the peer is before the hello -- which carries
        the licence token, the machine UUID and the hostname -- goes out.
        """
        ctx = tls.client_ssl_context(tls.SERVER_HOST)

        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_the_LAN_path_is_deliberately_still_trust_on_first_use(self):
        """Not an oversight. No certificate for the public address can
        validate a local one, and the local address varies by network, so the
        LAN path pins on first sight at the application layer instead --
        `peer_is_acceptable` -> `verify_or_pin`."""
        ctx = tls.client_ssl_context("192.168.0.42")

        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    def test_the_internet_path_needs_no_fingerprint(self):
        """TLS has already done the work. Demanding a pin on top would refuse
        a connection that is already authenticated."""
        ok, why = tls.peer_is_acceptable(tls.SERVER_HOST, "any-fingerprint")

        assert ok is True
        assert "authority" in why

    def test_an_impostor_certificate_is_REFUSED_on_the_internet_path(self, tmp_path):
        """The property the whole change exists for.

        A self-signed certificate for the right address -- exactly what an
        attacker on the network path would present -- must now fail the
        handshake. Before 014 it was accepted, and the licence token went to
        it.
        """
        import socket
        import ssl as _ssl
        import threading
        import datetime as _dt
        import ipaddress
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls.SERVER_HOST)])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(tls.SERVER_HOST)),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cp, kp = tmp_path / "impostor_cert.pem", tmp_path / "impostor_key.pem"
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))

        sctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(str(cp), str(kp))
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(1)
        port = sock.getsockname()[1]

        def _serve():
            try:
                c, _ = sock.accept()
                with sctx.wrap_socket(c, server_side=True):
                    pass
            except Exception:
                pass

        threading.Thread(target=_serve, daemon=True).start()

        ctx = tls.client_ssl_context(tls.SERVER_HOST)
        with pytest.raises(_ssl.SSLError):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname=tls.SERVER_HOST):
                    pass
        sock.close()

    def test_the_client_context_is_a_client_context(self):
        """A server-side protocol here fails the handshake in a way that looks
        like a network fault rather than a configuration error."""
        assert tls.client_ssl_context().protocol == ssl.PROTOCOL_TLS_CLIENT
