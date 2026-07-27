"""
Password protection for the TEST module.

The hash is stored as a plain hex file at forex_trader/test_signal/.passwd and
shipped with the app.  It is computed with PBKDF2-HMAC-SHA256 with the machine's
hardware fingerprint baked into the salt, so a hash produced on machine A cannot
be verified on machine B.

Effect when shipped:
  - .passwd exists  → recipient sees "Enter Password" but can never pass it.
  - .passwd missing → recipient sees "Set Password"; if they set one it only
    works on their machine and does not grant access to your test data.
"""

import hashlib
from pathlib import Path
from typing import Optional

from backend.src.config.licence.fingerprint import get_sha256

_ITERATIONS = 260_000
_PEPPER = b"FOREX_TEST_V1_"
_HASH_FILE = Path(__file__).parent / ".passwd"


def _salt() -> bytes:
    fp = get_sha256().encode("ascii", errors="replace")
    return _PEPPER + fp


def hash_password(password: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _salt(), _ITERATIONS)
    return dk.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    return hash_password(password) == stored_hash


def get_stored_hash() -> Optional[str]:
    """Read the hash from the shipped .passwd file; None if not yet set."""
    try:
        text = _HASH_FILE.read_text(encoding="ascii").strip()
        return text or None
    except FileNotFoundError:
        return None


def save_stored_hash(password: str) -> None:
    """Hash the password with this machine's fingerprint and write .passwd."""
    _HASH_FILE.write_text(hash_password(password), encoding="ascii")
