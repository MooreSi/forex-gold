"""Commentary — split from core/database.py.
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


def save_commentary(commentary: dict, trade_id: Optional[str], signal_id: Optional[str]) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_claude_commentary (ts,event_type,trade_id,signal_id,payload)"
            " VALUES (?,?,?,?,?)",
            (time.time(), commentary.get("commentary_type", ""), trade_id, signal_id,
             json.dumps(commentary)),
        )
