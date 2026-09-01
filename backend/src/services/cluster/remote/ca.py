"""A private certificate authority for the licence/admin channel.

bugs/014: this channel is encrypted but unauthenticated. Clients set
`verify_mode = CERT_NONE`, so the licence token goes to whoever answers on the
admin server's address. The sync channel was fixed with trust-on-first-use in
August 2026; this one was left, because the admin client is what recovers a
stranded install and a pin that refuses wrongly locks an admin out of the
recovery path itself.

Owner's decision, 2026-09-01: **a private CA for the internet path, TOFU for
the LAN path.**

Why a CA rather than pinning one certificate
--------------------------------------------
Clients trust the *issuer*, so the server certificate can be reissued -- new
VPS, new key, expiry -- without an app update. Pinning a fingerprint would
make every rotation a release, and losing the server key would lock out every
client until they updated.

Why a PRIVATE CA rather than Let's Encrypt
------------------------------------------
These clients only ever talk to this one admin server. Trusting the whole
public CA system is more trust than the job needs: with a private root, only
the owner's key can produce a certificate the app accepts, and a mis-issuance
anywhere in the public system is irrelevant. It also needs no domain name, and
it can cover LAN addresses, which no public CA will ever do.

Let's Encrypt does now issue certificates for bare IP addresses (July 2025),
but only as ~6-day certificates validated over HTTP-01/TLS-ALPN-01. A renewal
that silently fails locks out every client within days. That cliff is the
reason this is a private CA and not a public one.

The one thing this design asks of the owner
-------------------------------------------
**The CA private key must be kept offline.** Anyone holding it can mint a
certificate this app will trust. It is used once at setup and then only when a
server certificate is reissued, which is a once-in-years operation. It must
never live on the VPS: the VPS holds only the certificate the CA signed for
it, and a stolen server key lets an attacker impersonate that one server until
it is reissued -- it does not let them mint anything new.

Nothing here runs in the app. `tools/make_remote_ca.py` is the operator-facing
command; this module is the library it and the tests use.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Ten years. The root is what ships inside the app, so its expiry is a hard
# lockout for every client that has not updated -- exactly the cliff this
# design exists to avoid. Long enough that rotation is planned, not forced.
CA_VALID_DAYS = 3650

# Shorter on purpose: a server certificate is replaceable without an app
# update, so there is no reason to make it long-lived, and a shorter life
# bounds the damage from a stolen VPS key.
SERVER_VALID_DAYS = 825

CA_CERT_NAME = "ca_cert.pem"

# The authority shipped inside the build. Its PRESENCE is what switches
# verification on -- a build-time fact, not a runtime flag, so there is no
# setting an attacker or a confused user can flip to downgrade a client back to
# accepting anything. A build made before the cutover simply has no file here
# and behaves as it always did.
BUNDLED_CA = Path(__file__).with_name(CA_CERT_NAME)
CA_KEY_NAME = "ca_key.pem"


def _crypto():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    return x509, hashes, serialization, rsa, NameOID


def load_cert(path: Path):
    """Parse a PEM certificate off disk."""
    from cryptography import x509
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def _load_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def _write_key(path: Path, key) -> None:
    """Write a private key readable only by its owner.

    The mode is set before the bytes are written: creating the file
    world-readable and chmod-ing afterwards leaves a window, short but real, in
    which the key that mints trusted certificates is readable by anyone on the
    machine.
    """
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)


def init_ca(directory: Path) -> tuple[Path, Path]:
    """Create the root certificate and key. Returns (cert, key) paths.

    Refuses to overwrite an existing authority. Regenerating it invalidates
    every certificate it ever signed and every copy already shipped inside an
    app build, so it must never happen by accident.
    """
    x509, hashes, serialization, rsa, NameOID = _crypto()

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CA_CERT_NAME
    key_path = directory / CA_KEY_NAME
    if cert_path.exists() or key_path.exists():
        raise FileExistsError(
            f"a certificate authority already exists in {directory}. Creating a "
            f"second one invalidates every certificate this one signed and every "
            f"copy shipped in an app build. Move the existing files aside "
            f"deliberately if that is really what you want."
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FOREX Trader"),
        x509.NameAttribute(NameOID.COMMON_NAME, "FOREX Trader Admin Root CA"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))   # tolerate clock skew
        .not_valid_after(now + timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_key(key_path, key)
    return cert_path, key_path


def issue_server_cert(ca_cert_path: Path, ca_key_path: Path,
                      out_dir: Path, addresses: Iterable[str]) -> tuple[Path, Path]:
    """Sign a server certificate for *addresses*. Returns (cert, key) paths.

    `addresses` may mix IP addresses and hostnames; each goes into the subject
    alternative name, which is what verification actually matches on. A
    certificate carrying the address only in its common name verifies against
    nothing on any modern client.
    """
    x509, hashes, serialization, rsa, NameOID = _crypto()

    ca_cert = load_cert(ca_cert_path)
    ca_key = _load_key(ca_key_path)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cert_path = out_dir / "server_cert.pem"
    key_path = out_dir / "server_key.pem"

    alt: list = []
    primary = ""
    for value in addresses:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            alt.append(x509.DNSName(value))
        primary = primary or value
    if not alt:
        raise ValueError("a server certificate needs at least one address")
    # localhost, so the server can talk to itself and so a local smoke test
    # does not need the public address to resolve.
    alt.append(x509.DNSName("localhost"))
    alt.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, primary)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SERVER_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_key(key_path, key)
    return cert_path, key_path


def verify_context(ca_cert_path: Path) -> "object":
    """A client SSL context that trusts *only* this authority.

    `CERT_REQUIRED` plus `check_hostname` on purpose. Verifying the issuer
    without the address would accept a certificate signed by this CA for a
    different machine, which is still the wrong machine -- and this CA signs
    every server in the estate, so that is a real distinction rather than a
    theoretical one.

    The default trust store is deliberately NOT loaded. These clients only
    ever talk to this one admin server, so a certificate vouched for by any
    public CA is not something the app should accept.
    """
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_cert_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def bundled_ca_path():
    """The authority shipped with this build, or None if none is bundled.

    Never raises and never falls back to a system path: "no CA bundled" is a
    legitimate state (every build before the cutover) and must be reported as
    such rather than as an error.
    """
    try:
        return BUNDLED_CA if BUNDLED_CA.exists() else None
    except Exception:
        return None
