"""Data-retention pruning statements, collected out of utils/retention.py
(M1 SQL sweep). Table names and clauses come from retention.py's fixed
literal tuple, never from user input; one db() block prunes all tables
in a single commit, exactly as inline."""
from __future__ import annotations

from backend.src.db.database import db


def prune_tables(specs) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with db() as conn:
        for table, clause, param in specs:
            cur = conn.execute(f"DELETE FROM {table} WHERE {clause}", (param,))
            deleted[table] = cur.rowcount
    return deleted
