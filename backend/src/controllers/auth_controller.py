"""Dashboard login — thin forwarder to the auth service."""
from __future__ import annotations

from backend.src import config as _config
from backend.src.services.auth import dashboard_auth as _auth

__all__ = ["verify", "is_set", "set_password", "is_debug"]


def verify(username: str, password: str) -> bool:
    return _auth.verify(username, password)


def is_debug() -> bool:
    """Exposed so the frontend can show the debug login hint without importing
    config directly (keeps the frontend→controllers boundary)."""
    return bool(_config.is_debug())


def is_set() -> bool:
    return _auth.is_set()


def set_password(password: str) -> None:
    _auth.set_password(password)
