"""Read-only access to the TEST signal generator's own database.

This is a **separate database file** from the trading app's — `test_signal.db`,
written by the signal-generator module. The monthly P&L calendar overlays its ADX
and HTF-bias samples to label each day's market type, which is the only reason
the history views reach outside their own data at all.

It gets a named adapter rather than folding into `trade_history_repo`. That is
not tidiness: the main repo's `db_module.db()` resolves to whichever database
`init()` last pointed at, so putting these queries there would silently read the
*trading* database for a table it does not have, and the surrounding
`try`/`except` in the calendar would swallow the resulting error and render an
empty overlay. A wrong-database read that fails silently is worse than the raw
`sqlite3.connect()` this replaces.

Read-only by construction: opened with SQLite's `mode=ro` URI, so a bug here
cannot write to the generator's data even by accident.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from backend.src.config import DATA_DIR

DB_NAME = "test_signal.db"


def _db_path() -> Path:
    return DATA_DIR / DB_NAME


def is_available() -> bool:
    """Whether the generator's database exists yet.

    Callers must check: the signal generator is optional, and on a fresh install
    the file is simply absent. That is a normal state, not an error.
    """
    return _db_path().exists()


def adx_and_bias_samples(ts_start: float, ts_end: float) -> list[dict[str, Any]]:
    """ADX and HTF-bias samples in `[ts_start, ts_end)`.

    Rows with a NULL adx are excluded here rather than filtered by the caller --
    they carry no market-type signal, and dropping them in SQL keeps the result
    set proportional to what the overlay actually uses.

    Returns dicts, never `sqlite3.Row`. A Row has no `.get()`, and a Row reaching
    a NiceGUI timer callback raises an AttributeError that NiceGUI swallows, so
    the page just stops refreshing with no traceback.
    """
    path = _db_path()
    if not path.exists():
        return []
    try:
        # mode=ro: this is somebody else's database and we only ever read it.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT ts, adx, htf_bias FROM test_analysis_log "
                "WHERE ts >= ? AND ts < ? AND adx IS NOT NULL",
                (ts_start, ts_end),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        # The generator may not have created its schema yet. An empty overlay is
        # the correct degraded state; the calendar renders without market types.
        return []
