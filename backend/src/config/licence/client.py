"""
Licence client — all HTTP communication with the auth server at 217.155.25.160.
Server URL is hardcoded and not user-configurable.
Certificate pinning via bundled server.crt.
"""
import logging
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ── Hardcoded server — not configurable ───────────────────────────────────────
_AUTH_SERVER = "https://217.155.25.160"
_TIMEOUT     = 10   # seconds

# Bundled server certificate for pinning (place at forex_trader/licence/server.crt).
# If the file is absent, falls back to system CA bundle (less secure but still HTTPS).
_CERT_PATH = Path(__file__).parent / "server.crt"


class LicenceServerError(Exception):
    """Raised when the auth server returns an error or is unreachable."""
    def __init__(self, message: str, reason: Optional[str] = None):
        super().__init__(message)
        self.reason = reason


def _client() -> httpx.Client:
    verify = str(_CERT_PATH) if _CERT_PATH.exists() else True
    return httpx.Client(verify=verify, timeout=_TIMEOUT)


def _post(path: str, payload: dict) -> dict:
    try:
        with _client() as c:
            resp = c.post(f"{_AUTH_SERVER}{path}", json=payload)
        if resp.status_code == 200:
            return resp.json()
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        detail = data.get("detail") or resp.text or f"HTTP {resp.status_code}"
        raise LicenceServerError(detail, reason=str(resp.status_code))
    except LicenceServerError:
        raise
    except httpx.ConnectError:
        raise LicenceServerError("Cannot reach the licence server. Check your internet connection.", reason="unreachable")
    except httpx.TimeoutException:
        raise LicenceServerError("Licence server timed out.", reason="timeout")
    except Exception as e:
        raise LicenceServerError(f"Unexpected error contacting licence server: {e}", reason="unknown")


def request_registration(email: str, fingerprint_hash: str) -> dict:
    """
    POST /register — tells the server to email a licence key to the address.
    Returns {"status": "key_sent", "server_time": ...} or raises LicenceServerError.
    """
    log.info("Requesting licence registration for %s", email)
    return _post("/register", {"email": email, "fingerprint_hash": fingerprint_hash})


def activate_licence(email: str, licence_key: str, fingerprint_hash: str) -> dict:
    """
    POST /activate — submits the emailed key, returns JWT on success.
    Returns {"status": "activated", "token": ..., "expires_at": ..., "server_time": ...}
    or raises LicenceServerError.
    """
    log.info("Activating licence for %s", email)
    return _post("/activate", {
        "email":            email,
        "licence_key":      licence_key,
        "fingerprint_hash": fingerprint_hash,
    })


def validate_token(email: str, token: str, fingerprint_hash: str) -> dict:
    """
    POST /validate — periodic check-in. Returns {"valid": bool, "new_token": ..., ...}
    or raises LicenceServerError on network failure.
    """
    return _post("/validate", {
        "email":            email,
        "token":            token,
        "fingerprint_hash": fingerprint_hash,
    })
