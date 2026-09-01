"""App Config — split from core/database.py.
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


def get_app_config(key: str) -> Optional[str]:
    """Lenient read: None for an unset key AND for a failed one.

    Deliberately unchanged -- around forty callers read optional configuration
    through this and expect None rather than an exception. Where the difference
    between "not set" and "could not read" MATTERS, use
    `read_app_config_strict` below.
    """
    try:
        with db() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def read_app_config_strict(key: str) -> Optional[str]:
    """Same read, but a database failure RAISES instead of looking unset.

    `get_app_config` cannot tell a caller which of the two happened, and for
    `trade_pause_until` that ambiguity means a locked database reports "not
    paused" and a halted account resumes trading. Callers that gate safety on
    a value read it through here and decide for themselves what an unreadable
    answer means. See governor.is_trading_paused.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_app_config(key: str, value: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)", (key, value)
            )
    except Exception as e:
        log.debug("app_config set error: %s", e)
