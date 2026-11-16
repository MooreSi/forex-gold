"""Feedback this install sent, until the sender has seen it marked Completed.

A row is awaiting until the issuer says it is completed, then a notice until
the dashboard's "Feedback noted and completed" popup is closed, then gone.
Keyed on the entry id, which is a random uuid4: an install can only ever ask
about, and be told about, its own feedback.
"""
from __future__ import annotations

import time
from pathlib import Path

from backend.src.config import USER_DATA_DIR
from backend.src.services.feedback import _jsonfile

_PATH = Path(USER_DATA_DIR) / "remote" / "feedback_sent.json"
#: Only a reminder of which piece of feedback it was, not the whole text.
EXCERPT = 200


def add(entry: dict) -> None:
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        if any(r.get("id") == entry["id"] for r in rows):
            return
        rows.append({"id": entry["id"], "kind": entry.get("kind", "feedback"),
                     "message": str(entry.get("message") or "")[:EXCERPT],
                     "submitted_at": entry.get("submitted_at", time.time()),
                     "completed_at": 0})
        _jsonfile.write(_PATH, rows)


def awaiting_ids() -> list[str]:
    return [str(r.get("id")) for r in _jsonfile.read(_PATH)
            if not r.get("completed_at") and r.get("id")]


def mark_completed(ids) -> None:
    """The issuer says these are done. Ids not awaiting here are ignored."""
    wanted = {str(i) for i in ids}
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        changed = False
        for r in rows:
            if r.get("id") in wanted and not r.get("completed_at"):
                r["completed_at"] = time.time()
                changed = True
        if changed:
            _jsonfile.write(_PATH, rows)


def notices() -> list[dict]:
    """Completed and not yet seen, oldest completion first."""
    rows = [r for r in _jsonfile.read(_PATH) if r.get("completed_at")]
    return sorted(rows, key=lambda r: r.get("completed_at", 0))


def mark_seen(entry_id: str) -> bool:
    """Drop a shown notice. False when there is no such notice."""
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        kept = [r for r in rows
                if not (r.get("id") == entry_id and r.get("completed_at"))]
        if len(kept) == len(rows):
            return False
        _jsonfile.write(_PATH, kept)
        return True
