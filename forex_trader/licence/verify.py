"""
Licence signature verification -- Ed25519, public-key only.

This module ships with every client install (imported by guard.py to
verify a licence key at startup). It can verify a signature was produced
by the matching private key, but cannot itself produce a new valid one --
that key lives only in KeyGen/licence_signing.py, on the admin's own
machine, never committed to this (public) repo and never shipped to any
client.

Replaces the old keygen.py, which held a single shared HMAC-SHA256 secret
used for BOTH generation and verification in the same file every client
received -- meaning anyone with a copy of the app (and, once this repo
went public, literally anyone) could mint their own valid licence keys.

Algorithm:
  signature = Ed25519_sign(private_key, "{machine_id}|{expiry_date}|2.0")
  formatted as 16 groups of 8 uppercase hex chars (128 hex chars total).
"""
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_VERSION = "2.0"

# Public key only -- safe to ship. Matches the private key in
# KeyGen/licence_signing.py (admin-only, never committed here).
_PUBLIC_KEY_HEX = "4dd6b4c47ef9fc318248b92a609d70908354eea2f4cbca9ec1f96472dd276be9"

_public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_PUBLIC_KEY_HEX))


def verify_licence_key(machine_id: str, expiry_date: str, licence_key: str) -> bool:
    """Return True if licence_key is a valid Ed25519 signature for this
    machine_id / expiry_date combination."""
    try:
        sig_hex = licence_key.upper().strip().replace("-", "")
        signature = binascii.unhexlify(sig_hex)
        payload = f"{machine_id}|{expiry_date}|{_VERSION}".encode()
        _public_key.verify(signature, payload)
        return True
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False
