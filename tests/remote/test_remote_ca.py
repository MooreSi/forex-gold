"""A private CA for the licence/admin channel.

bugs/014: the admin channel is encrypted but unauthenticated -- clients accept
any certificate, so the licence token travels to whoever answers on
217.155.25.160:8443. The sync channel was fixed with trust-on-first-use in
August; this channel was left because a pin that refuses wrongly locks an
admin out of the recovery path itself.

The owner's decision, 2026-09-01: **a private CA for the internet path, TOFU
for the LAN path.**

Why a CA rather than pinning one certificate: the server certificate can then
be reissued -- new VPS, new key, expiry -- without an app update, because
clients trust the issuer and not that one certificate. And why a PRIVATE CA
rather than a public one: these clients only ever talk to this one admin
server, so trusting the whole public CA system is more trust than the job
needs. Only the owner's key can produce a certificate the app accepts.

Everything here is exercised against real certificates produced by the real
code. Nothing is mocked: a mocked verification test passes while verification
is broken, which is the exact shape of the bug being fixed.
"""
from __future__ import annotations

import ipaddress

import os

import pytest

from backend.src.services.cluster.remote import ca as remote_ca

@pytest.fixture
def ca_dir(tmp_path):
    return tmp_path / "ca"


class TestCreatingTheAuthority:
    def test_it_writes_a_key_and_a_certificate(self, ca_dir):
        cert, key = remote_ca.init_ca(ca_dir)

        assert cert.exists() and key.exists()

    def test_the_certificate_is_a_certificate_authority(self, ca_dir):
        """Without basicConstraints CA:TRUE, OpenSSL will not accept anything
        it signs, and the failure appears at connection time as an opaque
        verify error."""
        cert, _ = remote_ca.init_ca(ca_dir)
        loaded = remote_ca.load_cert(cert)

        from cryptography import x509
        basic = loaded.extensions.get_extension_for_class(x509.BasicConstraints)

        assert basic.value.ca is True
        assert basic.critical is True

    def test_it_may_not_sign_anything_but_certificates(self, ca_dir):
        cert, _ = remote_ca.init_ca(ca_dir)
        loaded = remote_ca.load_cert(cert)

        from cryptography import x509
        usage = loaded.extensions.get_extension_for_class(x509.KeyUsage).value

        assert usage.key_cert_sign is True
        assert usage.digital_signature is False

    def test_it_is_long_lived(self, ca_dir):
        """The root is the thing shipped inside the app. If it expires, every
        client is locked out until they update -- the cliff this design exists
        to avoid. Ten years, so rotation is planned rather than forced."""
        cert, _ = remote_ca.init_ca(ca_dir)
        loaded = remote_ca.load_cert(cert)

        years = (loaded.not_valid_after_utc - loaded.not_valid_before_utc).days / 365.25

        assert years >= 9

    def test_it_refuses_to_overwrite_an_existing_authority(self, ca_dir):
        """Regenerating the CA invalidates every certificate it ever signed
        and every copy shipped in an app build. It must never happen by
        accident."""
        remote_ca.init_ca(ca_dir)

        with pytest.raises(FileExistsError):
            remote_ca.init_ca(ca_dir)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX file modes. Windows ignores chmod's permission bits, so "
               "the key lands 0o666 there and no chmod will change it -- the "
               "same limitation as the bridge credentials file. Windows is "
               "protected by an ACL instead (utils/file_perms), verified "
               "there by a Windows-only test on CI; this assertion remains "
               "the right check on POSIX.",
    )
    def test_the_key_is_not_world_readable(self, ca_dir):
        """Anyone holding this key can mint a certificate the app trusts."""
        import stat
        _, key = remote_ca.init_ca(ca_dir)

        mode = stat.S_IMODE(key.stat().st_mode)

        assert mode & 0o077 == 0, oct(mode)


class TestIssuingAServerCertificate:
    def test_it_is_signed_by_the_authority(self, ca_dir, tmp_path):
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        cert, _key = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7"])

        issued = remote_ca.load_cert(cert)
        root = remote_ca.load_cert(ca_cert)

        assert issued.issuer == root.subject

    def test_the_ip_is_in_the_subject_alternative_name(self, ca_dir, tmp_path):
        """Verification matches on SAN, not on the common name. A certificate
        with the address only in the CN verifies against nothing."""
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        cert, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7"])

        from cryptography import x509
        san = remote_ca.load_cert(cert).extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value

        assert ipaddress.IPv4Address("203.0.113.7") in san.get_values_for_type(
            x509.IPAddress)

    def test_several_addresses_can_share_one_certificate(self, ca_dir, tmp_path):
        """The admin server is reachable on its public address and, on a LAN,
        on a local one. A private CA can cover both, which no public CA will
        ever do."""
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        cert, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7", "192.168.1.50"])

        from cryptography import x509
        san = remote_ca.load_cert(cert).extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        ips = san.get_values_for_type(x509.IPAddress)

        assert ipaddress.IPv4Address("192.168.1.50") in ips

    def test_a_hostname_can_be_included_too(self, ca_dir, tmp_path):
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        cert, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7", "admin.example.com"])

        from cryptography import x509
        san = remote_ca.load_cert(cert).extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value

        assert "admin.example.com" in san.get_values_for_type(x509.DNSName)

    def test_it_is_not_itself_an_authority(self, ca_dir, tmp_path):
        """A server certificate that could sign others would turn one stolen
        VPS key into the ability to mint trusted certificates."""
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        cert, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7"])

        from cryptography import x509
        basic = remote_ca.load_cert(cert).extensions.get_extension_for_class(
            x509.BasicConstraints).value

        assert basic.ca is False

    def test_reissuing_produces_a_different_certificate(self, ca_dir, tmp_path):
        """The whole point of a CA over a pinned fingerprint: the server
        certificate can be replaced and clients keep working."""
        ca_cert, ca_key = remote_ca.init_ca(ca_dir)
        first, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "a", ["203.0.113.7"])
        second, _ = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "b", ["203.0.113.7"])

        a = remote_ca.load_cert(first)
        b = remote_ca.load_cert(second)

        assert a.serial_number != b.serial_number


# ── The part that cannot be mocked ───────────────────────────────────────────
#
# Everything above checks the shape of the certificates. None of it proves
# OpenSSL will accept one and refuse another, and that is the entire point of
# the change. A mocked verification test passes while verification is broken --
# which is precisely how bugs/014 went unnoticed: a safeguard that looked
# present and was not.
#
# So these run a real TLS handshake over a real socket, with the real contexts
# the app builds.

import socket
import ssl
import threading


def _serve_once(cert_path, key_path, ready, result):
    """A one-shot TLS server. Records whether the handshake completed."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    ready.append(sock.getsockname()[1])
    ready.append(True)
    try:
        raw, _ = sock.accept()
        try:
            with ctx.wrap_socket(raw, server_side=True):
                result.append("handshake ok")
        except Exception as e:
            result.append(f"server-side failure: {type(e).__name__}")
    except OSError:
        pass
    finally:
        sock.close()


def _try_connect(port, client_ctx, server_hostname):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with client_ctx.wrap_socket(raw, server_hostname=server_hostname) as tls:
            return tls.getpeercert()


class _Server:
    def __init__(self, cert, key):
        self.ready: list = []
        self.result: list = []
        self._t = threading.Thread(
            target=_serve_once, args=(cert, key, self.ready, self.result), daemon=True)
        self._t.start()
        while len(self.ready) < 2:
            pass
        self.port = self.ready[0]

    def wait(self, timeout=5.0):
        """Let the server thread finish recording.

        The client's wrap_socket returns as soon as ITS side of the handshake
        completes, which can be before the server thread has appended its
        result. Joining removes that race -- without it the acceptance test
        fails intermittently on a working implementation, which is worse than
        no test.
        """
        self._t.join(timeout)
        return self.result


@pytest.fixture
def authority(tmp_path):
    ca_cert, ca_key = remote_ca.init_ca(tmp_path / "ca")
    return ca_cert, ca_key


class TestARealHandshake:
    def test_a_certificate_from_our_ca_is_accepted(self, authority, tmp_path):
        ca_cert, ca_key = authority
        cert, key = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["127.0.0.1"])
        server = _Server(cert, key)

        peer = _try_connect(server.port, remote_ca.verify_context(ca_cert), "127.0.0.1")

        assert peer, "the handshake produced no peer certificate"
        assert server.wait() == ["handshake ok"]

    def test_a_certificate_from_a_DIFFERENT_ca_is_refused(self, authority, tmp_path):
        """The whole point. An impostor with a valid-looking certificate from
        its own authority must not be accepted."""
        ca_cert, _ = authority
        other_cert, other_key = remote_ca.init_ca(tmp_path / "impostor")
        cert, key = remote_ca.issue_server_cert(
            other_cert, other_key, tmp_path / "srv", ["127.0.0.1"])
        server = _Server(cert, key)

        with pytest.raises(ssl.SSLCertVerificationError):
            _try_connect(server.port, remote_ca.verify_context(ca_cert), "127.0.0.1")

    def test_todays_self_signed_certificate_is_refused(self, authority, tmp_path):
        """What the admin server presents right now. After the cutover it must
        stop being accepted -- otherwise the change achieves nothing."""
        from backend.src.services.cluster.remote import tls as remote_tls
        ca_cert, _ = authority
        import backend.src.config as cfg
        monkey_dir = tmp_path / "selfsigned"
        monkey_dir.mkdir()

        # Build a self-signed cert the same way tls.ensure_cert does.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime as _dt
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + _dt.timedelta(days=30))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cp = monkey_dir / "c.pem"; kp = monkey_dir / "k.pem"
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        server = _Server(cp, kp)

        with pytest.raises(ssl.SSLCertVerificationError):
            _try_connect(server.port, remote_ca.verify_context(ca_cert), "127.0.0.1")

    def test_an_address_not_in_the_certificate_is_refused(self, authority, tmp_path):
        """Verification checks the address as well as the issuer. A
        certificate for a different machine, signed by the right CA, is still
        the wrong machine."""
        ca_cert, ca_key = authority
        cert, key = remote_ca.issue_server_cert(
            ca_cert, ca_key, tmp_path / "srv", ["203.0.113.7"])
        server = _Server(cert, key)

        ctx = remote_ca.verify_context(ca_cert)
        with pytest.raises(ssl.SSLCertVerificationError):
            _try_connect(server.port, ctx, "198.51.100.1")

    def test_verification_is_actually_switched_on(self, authority):
        """Negative control for every test above: if the context did not
        verify, the acceptance test would pass and so would nothing else."""
        ca_cert, _ = authority
        ctx = remote_ca.verify_context(ca_cert)

        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_the_system_trust_store_is_not_loaded(self):
        """The context must trust this app's authority and nothing else.

        Structural, and deliberately so. A behavioural version was written
        first and abandoned: on macOS `load_default_certs()` is effectively
        inert, so neither `get_ca_certs()` nor `cert_store_stats()` can tell a
        context that called it from one that did not -- both report exactly
        one certificate either way. On Windows, where this app actually ships,
        it is not inert. So the risk is real in production and invisible to
        any behavioural test that can run here, which makes reading the source
        the honest check rather than the lazy one.

        It matters because Let's Encrypt now issues certificates for bare IP
        addresses (July 2025). "A certificate some public CA vouched for" is a
        reachable state, not a theoretical one, and it is not what this app
        should accept.

        Found by mutation: adding `load_default_certs()` changed no test.
        """
        import pathlib as _p

        from backend.src.services.cluster.remote import ca as _ca

        src = _p.Path(_ca.__file__).read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines()
                if "load_default_certs" in ln and not ln.strip().startswith("#")]

        assert code == [], code

    def test_the_authority_it_does_trust_is_ours(self, authority):
        """Negative control for the structural test above, which would pass on
        a context that trusted nothing at all."""
        ca_cert, _ = authority
        ctx = remote_ca.verify_context(ca_cert)

        subject = dict(x[0] for x in ctx.get_ca_certs()[0]["subject"])

        assert subject["commonName"] == "FOREX Trader Admin Root CA"
