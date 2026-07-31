"""Ladder — split from core/database.py.
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


def create_ladder_leg(trade_id: str, tier: int, tp_num: int, tp_price: float,
                       lots: float, mt5_ticket: Optional[int],
                       entry_price: Optional[float] = None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO vantage_ladder_legs (trade_id,tier,tp_num,tp_price,lots,mt5_ticket,"
            "entry_price) VALUES (?,?,?,?,?,?,?)",
            (trade_id, tier, tp_num, tp_price, lots, mt5_ticket, entry_price),
        )
        return cur.lastrowid


def get_ladder_legs(trade_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM vantage_ladder_legs WHERE trade_id=? ORDER BY tier", (trade_id,)
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def close_ladder_leg(leg_id: int, close_price: float, pnl: float, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_ladder_legs SET status=?,close_price=?,close_time=?,pnl=? WHERE id=?",
            (status, close_price, time.time(), pnl, leg_id),
        )
