"""
Self-signed TLS certificate generation for the Local/Remote sync channel.

Same pattern as backend.src.controllers.remote.tls (self-signed cert + SHA-256
fingerprint pinning so a fixed-IP VPS doesn't need a real CA certificate),
but with its own cert/key files under USER_DATA_DIR/sync/ — deliberately
separate from remote/'s licence-server cert so the two channels can never
interfere with each other.

Unlike remote.tls, SYNC_HOST is not hardcoded: it's the VPS's own IP,
entered by the user in Settings > Remote Node and passed in at cert-gen time.
"""
from __future__ import annotations

import ipaddress
import logging
import ssl
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backend.src.config import USER_DATA_DIR

log = logging.getLogger(__name__)

_SYNC_DIR    = Path(USER_DATA_DIR) / "sync"
_CERT_FILE   = _SYNC_DIR / "sync_cert.pem"
_KEY_FILE    = _SYNC_DIR / "sync_key.pem"
_FPRINT_FILE = _SYNC_DIR / "sync_cert.fingerprint"

DEFAULT_SYNC_PORT = 8765


def ensure_cert(host: str) -> tuple[Path, Path]:
    """Generate a self-signed cert for `host` if one doesn't already exist.
    Returns (cert_path, key_path). Runs on the VPS (the server side) only."""
    _SYNC_DIR.mkdir(parents=True, exist_ok=True)

    if _CERT_FILE.exists() and _KEY_FILE.exists():
        return _CERT_FILE, _KEY_FILE

    log.info("[Sync-TLS] Generating self-signed certificate for %s …", host)
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now  = datetime.now(timezone.utc)

    san_entries: list = [x509.DNSName("localhost")]
    try:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        san_entries.append(x509.DNSName(host))  # host is a hostname, not an IP

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    _CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _KEY_FILE.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))

    fp = cert.fingerprint(hashes.SHA256()).hex(":")
    _FPRINT_FILE.write_text(fp, encoding="utf-8")
    log.info("[Sync-TLS] Certificate generated. Fingerprint: %s", fp)

    return _CERT_FILE, _KEY_FILE


def server_ssl_context(host: str) -> ssl.SSLContext:
    cert, key = ensure_cert(host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def client_ssl_context() -> ssl.SSLContext:
    """Accept the VPS's self-signed cert without CA verification — the caller
    is responsible for checking the fingerprint against the pinned value
    (see client.py's _verify_fingerprint) since hostname/CA checks are off."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def cert_fingerprint() -> str:
    """Return this machine's own sync-server cert fingerprint (call on the VPS
    to display/share the value the Mac should pin), or '' if not generated yet."""
    if _FPRINT_FILE.exists():
        return _FPRINT_FILE.read_text(encoding="utf-8").strip()
    return ""
