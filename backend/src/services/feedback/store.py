"""The issuer's record of feedback: what the admin console lists.

Keyed on the entry id, which the sending install generates, so a resend after
a dropped connection is recognised rather than recorded twice.
"""
from __future__ import annotations

import time
from pathlib import Path

from backend.src.config import USER_DATA_DIR
from backend.src.services.feedback import _jsonfile

_PATH = Path(USER_DATA_DIR) / "remote" / "feedback.json"


def add(entry: dict) -> bool:
    """Record `entry` as open. False when this id is already held."""
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        if any(r.get("id") == entry["id"] for r in rows):
            return False
        rows.append({**entry, "status": "open", "received_at": time.time(),
                     "completed_at": 0})
        _jsonfile.write(_PATH, rows)
        return True


def list_all() -> list[dict]:
    """Newest first."""
    rows = _jsonfile.read(_PATH)
    return sorted(rows, key=lambda r: r.get("submitted_at", 0), reverse=True)


def set_completed(entry_id: str, completed: bool) -> bool:
    """False when no such entry exists."""
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        for r in rows:
            if r.get("id") == entry_id:
                r["status"] = "completed" if completed else "open"
                r["completed_at"] = time.time() if completed else 0
                _jsonfile.write(_PATH, rows)
                return True
        return False
