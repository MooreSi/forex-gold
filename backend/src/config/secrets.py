"""
At-rest encryption for stored credentials.

The MT5 password (and any other secret run through encrypt()) is stored as a
Fernet token prefixed with "enc:v1:".  The Fernet key lives in the OS keychain
(macOS Keychain / Windows Credential Manager / Secret Service) via `keyring`.
When no keychain backend is available (headless install, Wine), the key falls
back to a 0600-permission file in USER_DATA_DIR so the DB alone never contains
enough to recover the password.

decrypt() passes non-prefixed values through unchanged, so legacy plaintext
rows keep working and are upgraded opportunistically on the next read.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
_KEYRING_SERVICE = "ForexTrader"
_KEYRING_ACCOUNT = "credentials-key"

_fernet: Optional[Fernet] = None


def _key_file_path() -> Path:
    from backend.src.config import USER_DATA_DIR
    return Path(USER_DATA_DIR) / ".credentials.key"


def _load_or_create_key() -> bytes:
    # Preferred: OS keychain
    try:
        import keyring
        stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        if stored:
            return stored.encode("utf-8")
        key = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key.decode("utf-8"))
        log.info("[Secrets] New encryption key stored in OS keychain")
        return key
    except Exception as e:
        log.warning("[Secrets] Keychain unavailable (%s) — using key file fallback", e)

    # Fallback: key file with owner-only permissions
    kf = _key_file_path()
    if kf.exists():
        return kf.read_bytes().strip()
    key = Fernet.generate_key()
    kf.parent.mkdir(parents=True, exist_ok=True)
    kf.write_bytes(key)
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    log.info("[Secrets] New encryption key stored at %s", kf)
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Empty values and already-encrypted tokens
    are returned unchanged."""
    if not plaintext or is_encrypted(plaintext):
        return plaintext or ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt a stored value. Plaintext (legacy) values pass through unchanged.
    An undecryptable token (key lost/changed) returns '' rather than garbage."""
    if not value:
        return ""
    if not is_encrypted(value):
        return value
    try:
        return _get_fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        log.error("[Secrets] Failed to decrypt stored credential — key mismatch?")
        return ""
