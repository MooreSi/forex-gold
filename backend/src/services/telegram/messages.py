"""Stored-message reads for the Telegram page."""
from __future__ import annotations

from typing import Any

from backend.src.db.database import to_db_thread
from backend.src.services.telegram import repo as _repo

__all__ = ["stored", "reader_status"]


def stored(limit: int = 100) -> tuple[list[dict], int]:
    try:
        return _repo.fetch_stored_messages(limit)
    except Exception:
        return [], 0


async def reader_status(reader: Any) -> dict:
    """reader.get_status() runs a SELECT COUNT(*) -- keep it off the loop."""
    return await to_db_thread(reader.get_status)
