"""Email — split from core/database.py.
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


def get_email_config() -> dict:
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM email_config WHERE id=1").fetchone())


def save_email_config(updates: dict) -> None:
    data = {**updates, "updated_at": time.time()}
    set_clause = ", ".join(f"{k}=?" for k in data)
    with db() as conn:
        conn.execute(
            f"UPDATE email_config SET {set_clause} WHERE id=1",
            list(data.values()),
        )


def fetch_closed_trades_since(cutoff: float) -> list[dict]:
    """Closed trades for the daily/weekly report emails (M1 SQL sweep)."""
    with db() as conn:
        return [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time>? "
                "ORDER BY close_time DESC",
                (cutoff,),
            ).fetchall()
        ]
