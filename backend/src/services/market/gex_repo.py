"""Storage for the daily GEX snapshots (docs/todo/009).

In reversal_engine.db, which is not per-environment: a history kept in the
core database would split in half the day the account switches, the reason
the decision log and the contradiction study live there too. The raw chain
is kept per strike so a later study can recompute under another sign
convention; the GEX figures are a convenience, not the record.
"""
from __future__ import annotations

import time
from typing import Optional

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gex_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at       REAL NOT NULL,
    asof_date      TEXT NOT NULL,
    underlying     TEXT NOT NULL,
    spot           REAL NOT NULL,
    xau_spot       REAL,
    ratio          REAL,
    total_gex      REAL,
    flip_level     REAL,
    call_wall      REAL,
    put_wall       REAL,
    xau_flip_level REAL,
    xau_call_wall  REAL,
    xau_put_wall   REAL,
    n_rows         INTEGER,
    expiries       TEXT,
    source         TEXT,
    assumptions    TEXT,
    UNIQUE(asof_date, underlying)
);
CREATE TABLE IF NOT EXISTS gex_strikes (
    snapshot_id INTEGER NOT NULL,
    expiry      TEXT NOT NULL,
    strike      REAL NOT NULL,
    t_years     REAL,
    call_oi     REAL,
    put_oi      REAL,
    call_iv     REAL,
    put_iv      REAL,
    call_gex    REAL,
    put_gex     REAL
);
CREATE INDEX IF NOT EXISTS idx_gex_strikes_snap ON gex_strikes(snapshot_id);
"""

_SNAP_COLS = ("asof_date", "underlying", "spot", "xau_spot", "ratio", "total_gex",
              "flip_level", "call_wall", "put_wall", "xau_flip_level", "xau_call_wall",
              "xau_put_wall", "n_rows", "expiries", "source", "assumptions")
_STRIKE_COLS = ("expiry", "strike", "t_years", "call_oi", "put_oi", "call_iv",
                "put_iv", "call_gex", "put_gex")


def create_schema() -> None:
    re_db.get_db().exec(_SCHEMA)


def insert_snapshot(snap: dict, strikes: list[dict]) -> Optional[int]:
    """One snapshot and its strikes, atomically. None if that day is stored."""
    db = re_db.get_db()
    with db.transaction():
        if db.get("SELECT id FROM gex_snapshots WHERE asof_date=? AND underlying=?",
                  snap["asof_date"], snap["underlying"]):
            return None
        res = db.run(
            f"INSERT INTO gex_snapshots (taken_at, {', '.join(_SNAP_COLS)}) "
            f"VALUES (?, {', '.join('?' for _ in _SNAP_COLS)})",
            time.time(), *[snap.get(c) for c in _SNAP_COLS],
        )
        sid = int(res.lastrowid)
        for s in strikes:
            db.run(
                f"INSERT INTO gex_strikes (snapshot_id, {', '.join(_STRIKE_COLS)}) "
                f"VALUES (?, {', '.join('?' for _ in _STRIKE_COLS)})",
                sid, *[s.get(c) for c in _STRIKE_COLS],
            )
    return sid


def latest() -> Optional[dict]:
    create_schema()   # a reader on a fresh install, before the first snapshot
    row = re_db.get_db().get("SELECT * FROM gex_snapshots ORDER BY asof_date DESC, id DESC LIMIT 1")
    return dict(row) if row else None


def strikes(snapshot_id: int) -> list[dict]:
    create_schema()
    return [dict(r) for r in re_db.get_db().all(
        "SELECT * FROM gex_strikes WHERE snapshot_id=? ORDER BY expiry, strike", snapshot_id)]


def count() -> int:
    create_schema()
    row = re_db.get_db().get("SELECT COUNT(*) AS n FROM gex_snapshots")
    return int(row["n"]) if row else 0
