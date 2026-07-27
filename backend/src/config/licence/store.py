"""
Local licence store — persists JWT token, licence key, and metadata
to a hidden JSON file in the user's home directory.
"""
import json
import os
import time
from pathlib import Path
from typing import Optional

STORE_PATH = Path.home() / ".forex_trader_licence"


def load() -> Optional[dict]:
    """Return stored licence data, or None if not present / unreadable."""
    try:
        if STORE_PATH.exists():
            return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def save(data: dict) -> None:
    """Persist licence data. Sets file permissions to owner-read-only."""
    STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(STORE_PATH, 0o600)
    except Exception:
        pass


def clear() -> None:
    """Delete stored licence data."""
    try:
        STORE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def update_token(new_token: str, token_expires_at: int, server_time: int) -> None:
    """Update just the JWT token fields in the store without rewriting everything."""
    data = load() or {}
    data["jwt_token"]         = new_token
    data["token_expires_at"]  = token_expires_at
    data["last_validated_at"] = server_time
    save(data)


def is_within_grace(data: dict, grace_days: int = 7) -> bool:
    """Return True if the stored JWT is still within the offline grace window."""
    expires = data.get("token_expires_at", 0)
    return time.time() < expires + (grace_days * 86400)
