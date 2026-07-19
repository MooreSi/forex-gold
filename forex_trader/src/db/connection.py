"""Module-level DbAdapter singleton -- generalizes the global-_DB_PATH
pattern gd_copy_signal/database.py already uses, for reuse across engines.
See docs/todo/refactor/backend-foundation/010-*.md.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .adapter import DbAdapter
from .sqlite_adapter import SqliteAdapter

_adapter: Optional[DbAdapter] = None


def init_db(db_path: str) -> DbAdapter:
    """Open a real SQLite file and make it the active adapter."""
    global _adapter
    con = sqlite3.connect(db_path, timeout=10)
    _adapter = SqliteAdapter(con)
    return _adapter


def get_db() -> DbAdapter:
    if _adapter is None:
        raise RuntimeError("init_db() (or set_db()) must be called before get_db()")
    return _adapter


def set_db(adapter: DbAdapter) -> None:
    """Swap in a specific adapter -- e.g. a fake/in-memory one for tests
    without going through init_db()'s real file-path connection."""
    global _adapter
    _adapter = adapter


def close_db() -> None:
    global _adapter
    if _adapter is not None and hasattr(_adapter, "close"):
        _adapter.close()
    _adapter = None
