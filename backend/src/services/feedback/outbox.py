"""Feedback this install has written but the issuer has not yet acknowledged.

An entry leaves only on an ack, so a submission made while the admin server is
unreachable is sent on the next connection instead of being lost.
"""
from __future__ import annotations

from pathlib import Path

from backend.src.config import USER_DATA_DIR
from backend.src.services.feedback import _jsonfile

_PATH = Path(USER_DATA_DIR) / "remote" / "feedback_outbox.json"


def add(entry: dict) -> None:
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        if not any(r.get("id") == entry["id"] for r in rows):
            rows.append(entry)
            _jsonfile.write(_PATH, rows)


def pending() -> list[dict]:
    return _jsonfile.read(_PATH)


def remove(entry_id: str) -> None:
    with _jsonfile.LOCK:
        rows = _jsonfile.read(_PATH)
        kept = [r for r in rows if r.get("id") != entry_id]
        if len(kept) != len(rows):
            _jsonfile.write(_PATH, kept)
