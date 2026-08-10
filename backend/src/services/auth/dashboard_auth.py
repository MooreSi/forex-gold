"""Single-user dashboard login.

The dashboard has no auth of its own (security review 2026-08-08, C1) — anyone
who can reach the port controls a live account. This adds a username/password
gate, hashed with scrypt (stdlib, no new dependency), the hash stored in the
user data dir and never shipped.

Debug convenience: when debug mode is on AND no password has been set, a fixed
`debug`/`debug` is accepted so local/e2e boots need no manual setup. This seed
NEVER applies with debug off, and never once a real password is set.
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from backend.src import config as _config
from backend.src.config import USER_DATA_DIR

_HASH_FILE = Path(USER_DATA_DIR) / "dashboard_password.hash"
_REAL_USERNAME = "admin"
_DEBUG_USERNAME = "debug"
_DEBUG_PASSWORD = "debug"
_SALT_LEN = 32


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def is_set() -> bool:
    return _HASH_FILE.exists()


def set_password(password: str, username: str = _REAL_USERNAME) -> None:
    """Store the scrypt hash of *password* (owner-read-only). Username is fixed
    to 'admin' for the real account; kept as a param for future multi-user."""
    salt = secrets.token_bytes(_SALT_LEN)
    _HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HASH_FILE.write_bytes(salt + _scrypt(password, salt))


def verify(username: str, password: str) -> bool:
    """True if the credentials are valid."""
    if not is_set():
        # No password configured yet. Only debug mode gets the convenience seed.
        return bool(_config.is_debug()) and username == _DEBUG_USERNAME and password == _DEBUG_PASSWORD
    if username != _REAL_USERNAME:
        return False
    raw = _HASH_FILE.read_bytes()
    salt, stored = raw[:_SALT_LEN], raw[_SALT_LEN:]
    return secrets.compare_digest(_scrypt(password, salt), stored)
