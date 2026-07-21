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

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


def get_app_config(key: str) -> Optional[str]:
    try:
        with db() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def set_app_config(key: str, value: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)", (key, value)
            )
    except Exception as e:
        log.debug("app_config set error: %s", e)
