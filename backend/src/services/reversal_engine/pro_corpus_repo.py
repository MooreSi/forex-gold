"""Shared storage for the reference-channel learning corpus (2026-08-06).

Named `*_repo` because that is what it is: every function here is CRUD on the
single `pro_snapshots` table -- create_schema, insert, exists, rows,
unresolved, set_cursor, set_outcome, counts, migrate_from_core. SQL belongs in
the data layer and the structure gate identifies that partly by filename, so a
storage module called `pro_corpus` reported seven statements as SQL leaking
into a service. Renamed 2026-08-27; no behaviour changed and every caller keeps
its `pro_corpus` alias.

WHY THIS LIVES IN reversal_engine.db AND NOT THE CORE DB
--------------------------------------------------------
core_signal_snapshot.py originally wrote tg_signal_snapshots into the CORE
database, which is per-environment: forex_trader_demo.db when the app runs
on the demo account, forex_trader_live.db on live. That splits the corpus in
half at exactly the moment it matters -- switching to live would have handed
the model an empty table and it would have silently learned nothing (the
live DB had zero snapshot rows on 2026-08-06, confirming it).

The Reversal Engine's own database is a single shared file regardless of
account environment (see app_lifecycle.startup), so what the professionals
did is recorded once and every environment trains on all of it. Demo vs live
is a trade-EXECUTION distinction, not a learning one.

WHAT A ROW IS
-------------
    stage != 'background'   a moment a reference channel actually fired
    stage == 'background'   the market on a timer, nobody fired

The contrast between the two is what makes "why then" learnable -- see
pro_model.py. Outcome columns (outcome/outcome_r/resolved_at) are filled in
later by pro_outcome.py walking candles forward from the stated entry; they
stay NULL until then, and NULL means "not yet known", never "no result".
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import sqlite3

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

log = logging.getLogger(__name__)

# Column order used by insert() and shared with core_signal_snapshot's writer.
_COLS = (
    "tg_message_id", "stage", "group_name", "direction", "signal_ts",
    "captured_at", "capture_lag_s", "entry_low", "entry_high", "stop_loss",
    "tp1", "bid", "ask", "spread_points", "price", "dist_to_entry_mid",
    "price_inside_zone", "session", "regime_score", "indicators_json",
    "fvg_json", "raw_text",
)


def create_schema() -> None:
    """Idempotent -- called on every startup after the RE db is opened."""
    re_db.get_db().exec("""
    CREATE TABLE IF NOT EXISTS pro_snapshots (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_message_id     TEXT NOT NULL,
        stage             TEXT NOT NULL,
        group_name        TEXT,
        direction         TEXT,
        signal_ts         REAL,
        captured_at       REAL NOT NULL,
        capture_lag_s     REAL,
        entry_low         REAL,
        entry_high        REAL,
        stop_loss         REAL,
        tp1               REAL,
        bid               REAL,
        ask               REAL,
        spread_points     REAL,
        price             REAL,
        dist_to_entry_mid REAL,
        price_inside_zone INTEGER,
        session           TEXT,
        regime_score      REAL,
        indicators_json   TEXT,
        fvg_json          TEXT,
        raw_text          TEXT,
        -- Walk-forward result of the call this row captured (pro_outcome.py).
        -- NULL until resolved. resolve_cursor_ts is how far the resolver has
        -- already walked, so each pass only reads candles it hasn't seen.
        outcome           TEXT,
        outcome_r         REAL,
        resolved_at       REAL,
        resolve_cursor_ts REAL,
        entry_fill_price  REAL,
        UNIQUE(tg_message_id, stage)
    );

    CREATE INDEX IF NOT EXISTS idx_pro_snap_stage ON pro_snapshots(stage);
    CREATE INDEX IF NOT EXISTS idx_pro_snap_ts    ON pro_snapshots(captured_at);
    CREATE INDEX IF NOT EXISTS idx_pro_snap_open  ON pro_snapshots(outcome, stage);
    """)


def insert(row: dict) -> bool:
    """Write one snapshot. Returns False if that (message, stage) already
    exists -- the UNIQUE constraint, not a pre-check, is what makes this
    safe against the capture poller racing itself."""
    marks = ",".join("?" for _ in _COLS)
    try:
        res = re_db.get_db().run(
            f"INSERT OR IGNORE INTO pro_snapshots ({','.join(_COLS)}) VALUES ({marks})",
            *[row.get(c) for c in _COLS],
        )
    except sqlite3.OperationalError as exc:
        log.debug("[ProCorpus] insert failed: %s", exc)
        return False
    return bool(getattr(res, "rowcount", 0))


def exists(tg_message_id: str, stage: str) -> bool:
    return re_db.get_db().get(
        "SELECT 1 FROM pro_snapshots WHERE tg_message_id=? AND stage=? LIMIT 1",
        tg_message_id, stage,
    ) is not None


def rows(background: bool) -> list[dict]:
    """Every positive (background=False) or negative (background=True) row,
    as dicts. Small enough to read whole: the corpus is a few hundred rows
    and both readers cache their result."""
    op = "=" if background else "!="
    out = re_db.get_db().all(
        f"SELECT direction, indicators_json, fvg_json, session, regime_score, "
        f"outcome, outcome_r, captured_at FROM pro_snapshots WHERE stage {op} 'background'"
    )
    return [dict(r) for r in out]


def unresolved(min_age_s: float = 300.0) -> list[dict]:
    """Positives with stated levels that still have no outcome. min_age_s
    keeps the resolver off a signal that has only just arrived, where there
    are no forward candles to judge it by yet."""
    cutoff = time.time() - min_age_s
    out = re_db.get_db().all(
        "SELECT id, tg_message_id, direction, signal_ts, entry_low, entry_high, "
        "stop_loss, tp1, resolve_cursor_ts, entry_fill_price "
        "FROM pro_snapshots "
        "WHERE stage != 'background' AND outcome IS NULL "
        "  AND stop_loss IS NOT NULL AND tp1 IS NOT NULL "
        "  AND entry_low IS NOT NULL AND entry_high IS NOT NULL "
        "  AND signal_ts < ? "
        "ORDER BY signal_ts",
        cutoff,
    )
    return [dict(r) for r in out]


def set_cursor(snap_id: int, cursor_ts: float,
               entry_fill_price: Optional[float] = None) -> None:
    re_db.get_db().run(
        "UPDATE pro_snapshots SET resolve_cursor_ts=?, "
        "entry_fill_price=COALESCE(?, entry_fill_price) WHERE id=?",
        cursor_ts, entry_fill_price, snap_id,
    )


def set_outcome(snap_id: int, outcome: str, outcome_r: Optional[float]) -> None:
    re_db.get_db().run(
        "UPDATE pro_snapshots SET outcome=?, outcome_r=?, resolved_at=? WHERE id=?",
        outcome, outcome_r, time.time(), snap_id,
    )


def counts() -> dict:
    r = re_db.get_db().get(
        "SELECT "
        " SUM(CASE WHEN stage='background' THEN 1 ELSE 0 END) AS neg,"
        " SUM(CASE WHEN stage!='background' THEN 1 ELSE 0 END) AS pos,"
        " SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,"
        " SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,"
        " SUM(CASE WHEN stage!='background' AND outcome IS NULL THEN 1 ELSE 0 END) AS pending"
        " FROM pro_snapshots"
    )
    d = dict(r) if r else {}
    return {k: int(d.get(k) or 0) for k in ("pos", "neg", "wins", "losses", "pending")}


# ── One-time import of the per-environment history ────────────────────────────

def migrate_from_core() -> int:
    """Copy any rows the old per-env core table already holds into the shared
    one. Idempotent via the UNIQUE constraint, so it is safe to run on every
    startup and safe to run again after switching demo/live -- which is the
    point: whichever environment boots next contributes whatever the other
    one recorded before this module existed.

    Returns how many new rows were taken. Never raises: a failed import must
    not stop the app starting.
    """
    try:
        from backend.src.db import database as core_db
        with core_db.db() as conn:
            got = conn.execute(
                f"SELECT {','.join(_COLS)} FROM tg_signal_snapshots"
            ).fetchall()
    except Exception as exc:
        # No such table on a fresh install or on the env that never ran the
        # old capture path -- nothing to import, and that is not an error.
        log.debug("[ProCorpus] nothing to import from core db: %s", exc)
        return 0

    n = 0
    for r in got:
        if insert({c: r[i] for i, c in enumerate(_COLS)}):
            n += 1
    if n:
        log.info("[ProCorpus] imported %d snapshot rows from the core database", n)
    return n


def init() -> None:
    create_schema()
    migrate_from_core()
