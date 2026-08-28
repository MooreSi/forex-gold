"""Dashboard login — thin forwarder to the auth service."""
from __future__ import annotations

from backend.src import config as _config
from backend.src.config.licence import fingerprint as _fingerprint
from backend.src.config.licence import store as _lic
from backend.src.services.auth import dashboard_auth as _auth

__all__ = ["verify", "is_set", "set_password", "is_debug",
           "needs_setup", "create_initial_password"]


def verify(username: str, password: str) -> bool:
    return _auth.verify(username, password)


def is_debug() -> bool:
    """Exposed so the frontend can show the debug login hint without importing
    config directly (keeps the frontend→controllers boundary)."""
    return bool(_config.is_debug())


def is_set() -> bool:
    return _auth.is_set()


def needs_setup() -> bool:
    return _auth.needs_setup()


def create_initial_password(password: str) -> bool:
    return _auth.create_initial_password(password)


def set_password(password: str) -> None:
    _auth.set_password(password)


# ── Licence ──────────────────────────────────────────────────────────────────

def load_licence() -> dict:
    """The stored licence record, or None/{} if this install has none.

    Read-only. Two settings panels show the licence holder and expiry; neither
    validates here -- that happens in the guard, not the UI.
    """
    return _lic.load()


def get_fingerprint() -> str:
    """This machine's hardware fingerprint, which a licence is issued against."""
    return _fingerprint.get_fingerprint()
