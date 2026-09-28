"""Storage for drawings on the Broker chart (docs/todo/011).

In reversal_engine.db, which is not per-environment: a support level on gold
is the same level on the demo and the live account, and a table in the core
database would split the drawings in half the day the account switches.
Points are JSON (time, price) pairs, never pixels.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_drawings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    points      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chart_drawings_symbol ON chart_drawings(symbol);
"""


def create_schema() -> None:
    re_db.get_db().exec(_SCHEMA)


def _out(row) -> dict:
    d = dict(row)
    d["points"] = json.loads(d["points"])
    return d


def list_for(symbol: str) -> list[dict]:
    create_schema()   # a reader on a fresh install, before the first drawing
    return [_out(r) for r in re_db.get_db().all(
        "SELECT * FROM chart_drawings WHERE symbol=? ORDER BY id", symbol)]


def count_for(symbol: str) -> int:
    create_schema()
    row = re_db.get_db().get("SELECT COUNT(*) AS n FROM chart_drawings WHERE symbol=?", symbol)
    return int(row["n"]) if row else 0


def insert(symbol: str, kind: str, points: list[dict]) -> dict:
    create_schema()
    now = time.time()
    db = re_db.get_db()
    res = db.run(
        "INSERT INTO chart_drawings (symbol, kind, points, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        symbol, kind, json.dumps(points), now, now,
    )
    return _out(db.get("SELECT * FROM chart_drawings WHERE id=?", int(res.lastrowid)))


def update_points(drawing_id: int, points: list[dict]) -> Optional[dict]:
    create_schema()
    db = re_db.get_db()
    res = db.run("UPDATE chart_drawings SET points=?, updated_at=? WHERE id=?",
                 json.dumps(points), time.time(), drawing_id)
    if not res.rowcount:
        return None
    return _out(db.get("SELECT * FROM chart_drawings WHERE id=?", drawing_id))


def delete(drawing_id: int) -> bool:
    create_schema()
    res = re_db.get_db().run("DELETE FROM chart_drawings WHERE id=?", drawing_id)
    return bool(res.rowcount)
