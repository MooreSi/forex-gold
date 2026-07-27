"""
Shared licence-key generation and verification — HMAC-SHA256.

Algorithm:
  key = HMAC-SHA256(_SERVER_SECRET, "{machine_id}|{expiry_date}|1.0")
  formatted as XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX

This module is imported by:
  - backend/src/config/licence/guard.py   (client — verifies on startup)
  - forex_trader/remote/server.py   (admin server — generates on approval)

The secret MUST also match /Users/simon/Documents/KeyGen/keygen.py.
"""
import hashlib
import hmac

_SERVER_SECRET = b"FOREX-SERVER-SECRET-CHANGEME-BEFORE-PRODUCTION"
_VERSION = "1.0"


def generate_licence_key(machine_id: str, expiry_date: str) -> str:
    """Generate the HMAC-SHA256 licence key for a given machine and expiry."""
    payload = f"{machine_id}|{expiry_date}|{_VERSION}".encode()
    raw = hmac.new(_SERVER_SECRET, payload, hashlib.sha256).hexdigest().upper()
    return "-".join(raw[i:i + 8] for i in range(0, 64, 8))


def verify_licence_key(machine_id: str, expiry_date: str, licence_key: str) -> bool:
    """Return True if licence_key is valid for this machine / expiry combination."""
    try:
        expected = generate_licence_key(machine_id, expiry_date)
        return hmac.compare_digest(
            expected.replace("-", ""),
            licence_key.upper().strip().replace("-", ""),
        )
    except Exception:
        return False
