"""Channel Parser — split from core/database.py.
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


# ── Channel parser config ─────────────────────────────────────────────────────

def get_channel_parser_config(channel_name: str) -> Optional[dict]:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM channel_parser_config WHERE channel_name=?", (channel_name,)
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def get_all_channel_parser_configs() -> list[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM channel_parser_config ORDER BY channel_name"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def save_channel_parser_config(
    channel_name: str,
    parser_format: str,
    signal_prefix: str,
    instant_entry_enabled: bool,
    enabled: bool,
    notes: str,
) -> None:
    now = time.time()
    with db() as conn:
        conn.execute(
            """INSERT INTO channel_parser_config
               (channel_name, parser_format, signal_prefix, instant_entry_enabled,
                enabled, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(channel_name) DO UPDATE SET
               parser_format=excluded.parser_format,
               signal_prefix=excluded.signal_prefix,
               instant_entry_enabled=excluded.instant_entry_enabled,
               enabled=excluded.enabled,
               notes=excluded.notes,
               updated_at=excluded.updated_at""",
            (channel_name, parser_format, signal_prefix,
             int(instant_entry_enabled), int(enabled), notes, now, now),
        )
