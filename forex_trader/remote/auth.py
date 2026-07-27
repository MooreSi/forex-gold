"""
Admin password hashing and verification.

Uses hashlib.scrypt (built-in, no extra dependency) for strong password hashing.
The hash file lives in USER_DATA_DIR and is never shipped with the app package —
so distributing the app to a remote user does not expose admin credentials.
"""

import hashlib
import os
import secrets
from pathlib import Path

from backend.src.config import USER_DATA_DIR

_HASH_FILE = Path(USER_DATA_DIR) / "remote" / "admin_password.hash"


def _scrypt_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,   # CPU/memory cost
        r=8,
        p=1,
        dklen=32,
    )


def set_password(password: str) -> None:
    """Hash and store the admin password.  Creates the directory if needed."""
    _HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    digest = _scrypt_hash(password, salt)
    # Format: hex(salt):hex(digest)
    _HASH_FILE.write_text(
        f"{salt.hex()}:{digest.hex()}", encoding="utf-8"
    )


def verify_password(password: str) -> bool:
    """Return True if *password* matches the stored hash."""
    if not _HASH_FILE.exists():
        return False
    try:
        text  = _HASH_FILE.read_text(encoding="utf-8").strip()
        salt_hex, digest_hex = text.split(":")
        salt   = bytes.fromhex(salt_hex)
        stored = bytes.fromhex(digest_hex)
        attempt = _scrypt_hash(password, salt)
        return secrets.compare_digest(attempt, stored)
    except Exception:
        return False


def password_is_set() -> bool:
    """True if the admin password has been configured on this machine."""
    return _HASH_FILE.exists() and _HASH_FILE.stat().st_size > 0
