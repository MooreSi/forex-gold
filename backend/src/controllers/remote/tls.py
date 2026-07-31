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


def client_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that accepts our self-signed cert without CA verification."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def cert_fingerprint() -> str:
    """Return the server cert SHA-256 fingerprint, or '' if cert doesn't exist yet."""
    if _FPRINT_FILE.exists():
        return _FPRINT_FILE.read_text(encoding="utf-8").strip()
    return ""
