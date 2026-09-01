"""
Self-signed TLS certificate generation and SSL context creation.

Cert and key are stored in USER_DATA_DIR/remote/ and generated once on first
admin-server startup.  They live outside the app bundle so they are not
distributed to remote users.
"""

import ipaddress
import logging
import ssl
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backend.src.config import USER_DATA_DIR

log = logging.getLogger(__name__)

_REMOTE_DIR  = Path(USER_DATA_DIR) / "remote"
_CERT_FILE   = _REMOTE_DIR / "server_cert.pem"
_KEY_FILE    = _REMOTE_DIR / "server_key.pem"
_FPRINT_FILE = _REMOTE_DIR / "server_cert.fingerprint"

SERVER_HOST = "217.155.25.160"
SERVER_PORT = 8443


def ensure_cert() -> tuple[Path, Path]:
    """Generate a self-signed cert if one doesn't exist.  Returns (cert, key) paths."""
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)

    if _CERT_FILE.exists() and _KEY_FILE.exists():
        return _CERT_FILE, _KEY_FILE

    log.info("[TLS] Generating self-signed certificate for %s …", SERVER_HOST)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_HOST)])
        now  = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.IPv4Address(SERVER_HOST)),
                    x509.DNSName("localhost"),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        _CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _KEY_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

        # Store fingerprint so clients can pin it
        fp = cert.fingerprint(hashes.SHA256()).hex(":")
        _FPRINT_FILE.write_text(fp, encoding="utf-8")
        log.info("[TLS] Certificate generated.  Fingerprint: %s", fp)

    except Exception as exc:
        log.error("[TLS] Certificate generation failed: %s", exc)
        raise

    return _CERT_FILE, _KEY_FILE


def server_ssl_context() -> ssl.SSLContext:
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


# ── Who this client is willing to talk to (bugs/014, stage 2) ────────────────
#
# The hello sent straight after connecting carries the licence token, the
# machine UUID and the hostname, so the peer has to be established before it
# goes out. Two paths, two mechanisms, on the owner's instruction 2026-09-01:
#
#   the internet path  verifies against the private CA bundled in the build
#   the LAN path       trust-on-first-use pins, because no certificate for the
#                      public address can validate a local one and the local
#                      address varies by network
#
# There is deliberately NO trust-on-first-use on the internet path. Pinning
# the current self-signed certificate there would mean that the day the
# CA-signed certificate is deployed, every already-updated client sees a
# mismatch and refuses -- a lockout created by the upgrade itself. That path
# goes straight from unauthenticated to CA-verified in one build.


def is_ca_verified(host: str) -> bool:
    """Did the TLS handshake itself establish who this peer is?

    True only for the internet path, and only in a build that ships an
    authority. Everything else -- LAN, localhost, and every build made before
    the cutover -- has to be checked at the application layer instead.
    """
    from backend.src.services.cluster.remote import ca as _ca
    return host == SERVER_HOST and _ca.bundled_ca_path() is not None


def client_ssl_context(host: str = SERVER_HOST) -> ssl.SSLContext:
    """The SSL context for connecting to *host*.

    Per host on purpose: one context reused for both paths would either refuse
    every LAN connection or verify neither.
    """
    from backend.src.services.cluster.remote import ca as _ca

    if is_ca_verified(host):
        return _ca.verify_context(_ca.bundled_ca_path())

    # Unchanged from before the cutover. The application layer does the
    # checking on this path -- see peer_is_acceptable.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def peer_is_acceptable(host: str, presented: str) -> tuple[bool, str]:
    """May the token be sent to this peer? Returns (ok, reason).

    On a CA-verified connection the answer is yes and no fingerprint is
    needed: TLS has already done the work, and demanding one on top would
    refuse a connection that is already authenticated.

    Otherwise the fingerprint must match what was pinned for this host, or be
    the first one seen. Reuses the sync channel's primitives rather than
    growing a second implementation of the same idea -- the pin store is
    keyed by host, so the two channels cannot collide.
    """
    if is_ca_verified(host):
        return True, "verified against the bundled certificate authority"

    from backend.src.services.cluster.sync import tls_util as _pin
    return _pin.verify_or_pin(host, presented)


def cert_fingerprint() -> str:
    """Return the server cert SHA-256 fingerprint, or '' if cert doesn't exist yet."""
    if _FPRINT_FILE.exists():
        return _FPRINT_FILE.read_text(encoding="utf-8").strip()
    return ""
