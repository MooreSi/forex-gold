"""Storage for the Breakout engine's tuning experiment ledger (docs/todo/007).

Lives in breakout_signal.db beside the params it records, like every other
table this engine owns. One row per AI adjustment.
"""
from __future__ import annotations

import time
from typing import Optional

from backend.src.services.breakout_signal.breakout_signal_repo import get_db
from backend.src.utils.sql_identifiers import set_clause_for

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bo_tuning_experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    param           TEXT    NOT NULL,
    old_value       REAL,
    new_value       REAL    NOT NULL,
    hypothesis      TEXT,
    summary         TEXT,
    status          TEXT    NOT NULL,
    concurrent      INTEGER NOT NULL DEFAULT 1,
    applied_at      REAL,
    baseline_n      INTEGER,
    baseline_mean   REAL,
    decided_at      REAL,
    after_n         INTEGER,
    after_mean      REAL,
    after_sum       REAL,
    verdict         TEXT
);
CREATE INDEX IF NOT EXISTS idx_bo_tuning_status ON bo_tuning_experiments(status);
"""

_FIELDS = (
    "old_value", "new_value", "hypothesis", "summary", "status", "concurrent",
    "applied_at", "baseline_n", "baseline_mean", "decided_at", "after_n",
    "after_mean", "after_sum", "verdict",
)


def create_schema() -> None:
    get_db().exec(_SCHEMA)


def insert(param: str, new_value: float, status: str, **fields) -> int:
    cols = ["created_at", "param", "new_value", "status"]
    vals = [time.time(), param, new_value, status]
    for k, v in fields.items():
        if k not in _FIELDS:
            raise KeyError(k)
        cols.append(k)
        vals.append(v)
    result = get_db().run(
        f"INSERT INTO bo_tuning_experiments ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})", *vals,
    )
    return int(result.lastrowid)


def update(exp_id: int, **fields) -> None:
    if not fields:
        return
    for k in fields:
        if k not in _FIELDS:
            raise KeyError(k)
    get_db().run(f"UPDATE bo_tuning_experiments SET {set_clause_for(fields)} WHERE id=?",
                 *fields.values(), exp_id)


def get(exp_id: int) -> Optional[dict]:
    row = get_db().get("SELECT * FROM bo_tuning_experiments WHERE id=?", exp_id)
    return dict(row) if row else None


def with_status(*statuses: str) -> list[dict]:
    marks = ", ".join("?" for _ in statuses)
    rows = get_db().all(
        f"SELECT * FROM bo_tuning_experiments WHERE status IN ({marks}) ORDER BY id",
        *statuses,
    )
    return [dict(r) for r in rows]


def recent(exclude: tuple[str, ...], limit: int = 30) -> list[dict]:
    marks = ", ".join("?" for _ in exclude)
    rows = get_db().all(
        f"SELECT * FROM bo_tuning_experiments WHERE status NOT IN ({marks}) "
        "ORDER BY id DESC LIMIT ?", *exclude, limit,
    )
    return [dict(r) for r in rows]


def closed_net_since(ts: float) -> list[float]:
    """Net $ of every signal closed at or after `ts`, oldest first."""
    rows = get_db().all(
        "SELECT COALESCE(net_pnl_dollars, pnl_dollars, 0) FROM bo_signals "
        "WHERE status='closed' AND close_time >= ? ORDER BY close_time", ts,
    )
    return [float(r[0]) for r in rows]


def closed_net_before(ts: float, limit: int) -> list[float]:
    """Net $ of the last `limit` signals closed before `ts`."""
    rows = get_db().all(
        "SELECT COALESCE(net_pnl_dollars, pnl_dollars, 0) FROM bo_signals "
        "WHERE status='closed' AND close_time < ? ORDER BY close_time DESC LIMIT ?",
        ts, limit,
    )
    return [float(r[0]) for r in rows]
