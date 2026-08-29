"""
Self-signed TLS certificate generation for the Local/Remote sync channel.

Same pattern as backend.src.services.cluster.remote.tls (self-signed cert + SHA-256
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

# ── Certificate pinning (bugs/014) ───────────────────────────────────────────
# The client context above deliberately does not verify: there is no CA and the
# server is a bare IP. That is only safe if the caller compares the presented
# certificate against one it already knows, and until 2026-08-28 nothing did --
# this module's docstring named a `client.py::_verify_fingerprint` that never
# existed. The token is sent on the first frame after the handshake, so an
# unverified peer was handed it.
#
# Trust-on-first-use: the first fingerprint seen for a host is stored and
# accepted, and every later connection must match it. TOFU keeps an
# already-paired Mac/VPS working across this change -- it pins whatever they
# are already talking to -- at the cost of leaving that first connection
# exposed. That limit is real and is recorded in 014.

def _pin_key(host: str) -> str:
    return f"sync_pinned_fp_{host}"


def pinned_fingerprint(host: str) -> str:
    """The fingerprint this node has committed to for `host`, or ""."""
    from backend.src.db import database as db_module
    try:
        return db_module.get_app_config(_pin_key(host)) or ""
    except Exception:
        return ""


def clear_pin(host: str) -> None:
    """Forget the pin for `host`, so the next connection pins afresh.

    The recovery path for a genuinely reissued certificate. Without one, a
    legitimately rotated cert would lock the user out with no route back --
    and ensure_cert() never rotates on its own, so this should be rare.
    """
    from backend.src.db import database as db_module
    try:
        db_module.set_app_config(_pin_key(host), "")
    except Exception as e:
        log.warning("[TLS] could not clear pinned fingerprint for %s: %s", host, e)


def peer_fingerprint(ws) -> str:
    """SHA-256 of the certificate the peer actually presented, or "".

    Reaches through the websockets connection to the live SSL object. That
    path is a library internal, so it is proved against a real handshake in
    tests/core/test_sync_cert_pinning.py rather than mocked -- a mock here
    would pass while the real attribute path was wrong, which is precisely how
    014 went unnoticed.

    Returns "" rather than raising: this is called on the connect path, and a
    caller that cannot read a certificate must refuse the connection, not
    crash the reconnect loop.
    """
    import hashlib
    try:
        ssl_object = ws.transport.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True)
        if not der:
            return ""
        digest = hashlib.sha256(der).hexdigest()
        # Colon-separated hex, matching cert_fingerprint()'s own format so the
        # pinned value and the value shown on the Remote Node screen are
        # directly comparable by eye.
        return ":".join(digest[i:i + 2] for i in range(0, 64, 2))
    except Exception as e:
        log.warning("[TLS] could not read the peer certificate: %s", e)
        return ""


def verify_or_pin(host: str, presented: str) -> tuple[bool, str]:
    """Check a presented fingerprint against the pin, or set the pin.

    Returns (ok, reason). A refusal never overwrites the stored pin -- doing
    so would let a second attempt from the same wrong peer succeed.

    Plain equality is correct here: a certificate fingerprint is a public
    value, not a secret, so there is nothing for a timing comparison to leak.
    The shared token is the secret, and that is compared with compare_digest
    on the server side.
    """
    from backend.src.db import database as db_module

    if not presented:
        return False, ("the server presented no readable certificate — refusing "
                       "before sending the token")

    stored = pinned_fingerprint(host)
    if not stored:
        try:
            db_module.set_app_config(_pin_key(host), presented)
        except Exception as e:
            return False, f"could not store the certificate fingerprint: {e}"
        log.warning(
            "[TLS] pinned %s on first connection: %s. Every later connection "
            "must present this same certificate.", host, presented,
        )
        return True, "pinned on first connection"

    if stored == presented:
        return True, "certificate matches the pinned fingerprint"

    return False, (
        f"certificate fingerprint does not match the pinned value for {host}. "
        f"Expected {stored}, got {presented}. Refusing to send the sync token. "
        f"If the VPS certificate was genuinely reissued, re-enter the "
        f"connection details in Settings > Remote Node to pair again."
    )

