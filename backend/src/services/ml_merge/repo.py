"""The imported rows and the per-kind switch, in the Reversal engine's database.

That database already holds `pro_snapshots` and is the same file whichever
MT5 environment is selected, which is right for training history: a model's
rows do not change with the account it trades on.

The tables are created on the first write. A read before that (every install
that has never merged) finds no table and answers "nothing imported, switch
off", which is the unchanged behaviour.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from backend.src.services.reversal_engine import reversal_engine_repo as re_repo

KINDS = ("re", "bo", "pro")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_imported_rows (
    kind        TEXT    NOT NULL,
    origin_node TEXT    NOT NULL,
    origin_id   INTEGER NOT NULL,
    row_time    REAL    NOT NULL,
    row_json    TEXT    NOT NULL,
    imported_at REAL    NOT NULL,
    PRIMARY KEY (kind, origin_node, origin_id)
);
CREATE TABLE IF NOT EXISTS ml_merge_state (
    kind         TEXT PRIMARY KEY,
    use_imported INTEGER NOT NULL DEFAULT 0,
    report_json  TEXT,
    decided_at   REAL
);
"""


def _db():
    return re_repo.get_db()


def create_schema() -> None:
    _db().exec(_SCHEMA)


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown training dataset {kind!r}")


def insert_rows(kind: str, origin_node: str, rows: list[tuple[int, float, dict]]) -> int:
    """(origin_id, row_time, row) triples. Returns how many were new."""
    _check(kind)
    create_schema()
    now = time.time()
    added = 0
    with _db().transaction() as db:
        for origin_id, row_time, row in rows:
            added += db.run(
                "INSERT OR IGNORE INTO ml_imported_rows "
                "(kind, origin_node, origin_id, row_time, row_json, imported_at) "
                "VALUES (?,?,?,?,?,?)",
                kind, origin_node, int(origin_id), float(row_time),
                json.dumps(row, default=str), now,
            ).rowcount
    return added


def imported(kind: str) -> list[dict]:
    """Every imported row of `kind`, oldest first."""
    _check(kind)
    try:
        got = _db().all(
            "SELECT row_json FROM ml_imported_rows WHERE kind=? "
            "ORDER BY row_time, origin_node, origin_id", kind)
    except sqlite3.OperationalError:
        return []
    return [json.loads(r[0]) for r in got]


def count(kind: str) -> int:
    _check(kind)
    try:
        return int(_db().get(
            "SELECT COUNT(*) FROM ml_imported_rows WHERE kind=?", kind)[0])
    except sqlite3.OperationalError:
        return 0


def set_decision(kind: str, use: bool, report: dict) -> None:
    _check(kind)
    create_schema()
    _db().run(
        "INSERT INTO ml_merge_state (kind, use_imported, report_json, decided_at) "
        "VALUES (?,?,?,?) ON CONFLICT(kind) DO UPDATE SET "
        "use_imported=excluded.use_imported, report_json=excluded.report_json, "
        "decided_at=excluded.decided_at",
        kind, 1 if use else 0, json.dumps(report, default=str), time.time(),
    )


def decision(kind: str) -> Optional[dict]:
    """The last benefit check's report for `kind`, or None."""
    _check(kind)
    try:
        row = _db().get(
            "SELECT use_imported, report_json, decided_at FROM ml_merge_state WHERE kind=?",
            kind)
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {**json.loads(row[1] or "{}"), "use": bool(row[0]), "decided_at": row[2]}


def use_imported(kind: str) -> bool:
    """Whether training reads `kind`'s imported rows. Off on any failure."""
    try:
        row = _db().get("SELECT use_imported FROM ml_merge_state WHERE kind=?", kind)
    except Exception:
        return False
    return bool(row and row[0])
