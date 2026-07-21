"""Spread Cache — split from core/database.py.
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


def get_cached_spreads(position_ids: list[int]) -> dict[int, dict]:
    """{position_id: {spread_price, spread_points, spread_cost_usd}} for already-computed tickets."""
    if not position_ids:
        return {}
    try:
        with db() as conn:
            placeholders = ",".join("?" * len(position_ids))
            rows = conn.execute(
                f"SELECT position_id, spread_price, spread_points, spread_cost_usd "
                f"FROM trade_spread_cache WHERE position_id IN ({placeholders})",
                position_ids,
            ).fetchall()
        return {
            r[0]: {"spread_price": r[1], "spread_points": r[2], "spread_cost_usd": r[3]}
            for r in rows
        }
    except Exception:
        return {}


def cache_spread(position_id: int, spread_price: float, spread_points: float, spread_cost_usd: float) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trade_spread_cache "
                "(position_id, spread_price, spread_points, spread_cost_usd, computed_at) "
                "VALUES (?,?,?,?,?)",
                (position_id, spread_price, spread_points, spread_cost_usd, time.time()),
            )
    except Exception as e:
        log.debug("cache_spread error: %s", e)
