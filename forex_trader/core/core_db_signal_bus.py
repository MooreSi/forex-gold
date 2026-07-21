"""Signal Bus — split from core/database.py.
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


# ── Signal bus ────────────────────────────────────────────────────────────────
# Lightweight cross-engine awareness layer.  Each engine writes here when it
# generates a signal; all engines read here at ML-scoring time to get
# concurrent_agreement and conflict-suppression features.  Rows are auto-expired
# by the prune helper; the table never grows large.

def _ensure_signal_bus() -> None:
    """Idempotent: create the table if it was added after initial schema run."""
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_bus (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                engine        TEXT    NOT NULL,
                direction     TEXT    NOT NULL,
                confidence    REAL    NOT NULL DEFAULT 0.0,
                created_at    REAL    NOT NULL,
                expires_at    REAL    NOT NULL,
                is_still_open INTEGER NOT NULL DEFAULT 1,
                signal_id     INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_bus_exp ON signal_bus(expires_at)")
        # Migration: add new columns to existing tables
        for col, defn in [
            ("is_still_open", "INTEGER NOT NULL DEFAULT 1"),
            ("signal_id",     "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE signal_bus ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists


def write_signal_bus(
    engine: str,
    direction: str,
    confidence: float = 0.0,
    ttl_seconds: float = 300.0,
    signal_id: Optional[int] = None,
) -> int:
    """
    Record a signal on the shared bus. Returns the bus row id.
    TTL default reduced to 5 min (matches max scalp trade lifetime).
    Call close_bus_entry() when the originating signal closes so other engines
    see it as resolved immediately rather than waiting for TTL expiry.
    """
    try:
        _ensure_signal_bus()
        now = time.time()
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO signal_bus"
                "(engine,direction,confidence,created_at,expires_at,is_still_open,signal_id)"
                " VALUES(?,?,?,?,?,1,?)",
                (engine, direction.upper(), float(confidence), now, now + ttl_seconds, signal_id),
            )
            return cur.lastrowid or 0
    except Exception as _e:
        log.debug("[SignalBus] write error: %s", _e)
        return 0


def close_bus_entry(engine: str, signal_id: int) -> None:
    """
    Mark the bus entry for this engine+signal_id as no longer open.
    Call this as soon as the originating signal closes (SL/TP hit) so the
    conflict check clears immediately rather than waiting for TTL expiry.
    """
    try:
        _ensure_signal_bus()
        with db() as conn:
            conn.execute(
                "UPDATE signal_bus SET is_still_open=0 WHERE engine=? AND signal_id=?",
                (engine, signal_id),
            )
    except Exception as _e:
        log.debug("[SignalBus] close_bus_entry error: %s", _e)


def get_concurrent_signals(
    exclude_engine: str,
    window_seconds: float = 900.0,
) -> list[dict]:
    """
    Return active, still-open signals from all engines except the caller.
    is_still_open=0 entries (signal already closed) are excluded even if TTL has not expired.
    """
    try:
        _ensure_signal_bus()
        cutoff = time.time() - window_seconds
        with db() as conn:
            rows = conn.execute(
                "SELECT engine, direction, confidence, created_at FROM signal_bus"
                " WHERE engine != ? AND expires_at > ? AND created_at > ? AND is_still_open = 1",
                (exclude_engine, time.time(), cutoff),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_concurrent_agreement(engine: str, direction: str, window_seconds: float = 900.0) -> float:
    """
    Return cross-engine agreement score for use as an ML feature.
      +1.0  one or more other engines agree (same direction)
      -1.0  one or more other engines disagree (opposite direction)
       0.0  no concurrent signals from other engines
    Disagreement takes priority over agreement.
    """
    signals = get_concurrent_signals(exclude_engine=engine, window_seconds=window_seconds)
    if not signals:
        return 0.0
    direction_up = direction.upper()
    has_agree    = any(s["direction"] == direction_up for s in signals)
    has_disagree = any(s["direction"] != direction_up for s in signals)
    if has_disagree:
        return -1.0
    if has_agree:
        return 1.0
    return 0.0


def prune_signal_bus() -> None:
    """Delete expired rows. Call periodically (e.g. every 30 min from a tick loop)."""
    try:
        _ensure_signal_bus()
        with db() as conn:
            conn.execute("DELETE FROM signal_bus WHERE expires_at < ?", (time.time(),))
    except Exception:
        pass


def has_conflict_on_bus(engine: str, direction: str, window_seconds: float = 600.0) -> bool:
    """True if another engine has an active signal in the OPPOSITE direction."""
    signals = get_concurrent_signals(exclude_engine=engine, window_seconds=window_seconds)
    direction_up = direction.upper()
    return any(s["direction"] != direction_up for s in signals)
