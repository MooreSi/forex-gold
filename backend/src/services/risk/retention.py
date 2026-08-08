"""Data-retention window and environment switching.

Both are settings-page operations that reach the database layer rather than a
domain repo, so they get a service of their own rather than being smuggled
into the controller as a `db_module` import.

`switch_environment` is the one genuinely dangerous call here: `db.init()`
closes stale connections and flushes every registered cache, and the whole app
then reads a different file. It is a service function so the flush and the
re-point stay one operation.
"""
from __future__ import annotations

from backend.src.db import database as _db
from backend.src.db import retention as _retention

__all__ = ["get_days", "set_days", "prune", "switch_environment"]


def get_days() -> int:
    return _retention.get_data_retention_days()


def set_days(days: int) -> None:
    _retention.set_data_retention_days(days)


def prune() -> dict:
    return _retention.prune_historical_data()


def switch_environment(db_path: str) -> None:
    """Re-point the shared connection at another environment's DB file."""
    _db.init(db_path)
