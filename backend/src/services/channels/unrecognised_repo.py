"""Unrecognised — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from backend.src.db.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


# ── Unrecognised messages ─────────────────────────────────────────────────────

def save_unrecognised_message(channel_name: str, tg_message_id: str, raw_text: str) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO channel_unrecognised_messages
               (channel_name, tg_message_id, raw_text, received_at, status)
               VALUES (?,?,?,?,?)""",
            (channel_name, tg_message_id, raw_text, time.time(), "pending"),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM channel_unrecognised_messages WHERE tg_message_id=?",
            (tg_message_id,),
        ).fetchone()
        return row[0] if row else 0


def update_unrecognised_message(
    unrec_id: int,
    claude_analysis: Optional[str] = None,
    resolution: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    updates: dict = {}
    if claude_analysis is not None:
        updates["claude_analysis"] = claude_analysis
    if resolution is not None:
        updates["resolution"] = resolution
        updates["resolved_at"] = time.time()
    if status is not None:
        updates["status"] = status
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with db() as conn:
        conn.execute(
            f"UPDATE channel_unrecognised_messages SET {set_clause} WHERE id=?",
            list(updates.values()) + [unrec_id],
        )


def get_pending_unrecognised_messages(limit: int = 50) -> list[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT * FROM channel_unrecognised_messages
                   WHERE status='pending'
                   ORDER BY received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_all_unrecognised_messages(limit: int = 200) -> list[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT * FROM channel_unrecognised_messages
                   ORDER BY received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
