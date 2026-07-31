"""Custom Strategies — split from core/database.py.
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


def get_custom_strategies() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM custom_strategies ORDER BY created_at"
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def save_custom_strategy(s: dict) -> None:
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO custom_strategies
               (id, name, description, base_strategy, rules_json, created_at)
               VALUES (:id, :name, :description, :base_strategy, :rules_json, :created_at)""",
            s,
        )


def delete_custom_strategy(strategy_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM custom_strategies WHERE id=?", (strategy_id,))
