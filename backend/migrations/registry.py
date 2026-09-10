"""Schema migrations — an ordered, numbered registry, applied fail-closed.

Rules for changing this file:
- NEVER renumber, reorder, or edit an existing step — append a new one.
- Every SQL step must stay idempotent (ADD COLUMN / CREATE TABLE IF NOT
  EXISTS); apply_migration skips already-applied errors, aborts on the rest.

History and rationale: docs/system/domains/data/README.md.
"""
from __future__ import annotations

import sqlite3
import time

# Tables/columns the money path cannot run without. Verified after migration so
# a silently incomplete schema aborts startup instead of trading on it.
CRITICAL_SCHEMA = {
    "vantage_simulated_trades": {"trade_id", "managed_by", "order_type"},
    "vantage_risk_settings":    {"circuit_breaker_enabled", "re_live_execution"},
    "vantage_signals":          {"signal_id", "status"},
}


def apply_migration(conn, stmt: str) -> None:
    """Run one idempotent schema migration, failing closed on a real error.

    An already-applied migration raises 'duplicate column name' / 'already
    exists' — benign, skip it. ANY OTHER failure aborts startup: a genuinely
    failed migration must never be mistaken for an applied one, or the app
    trades on an unknown schema (review 2026-08-08, data #2). This is safe
    because _apply_schema runs CREATE TABLE IF NOT EXISTS for every table BEFORE
    this ADD COLUMN pass, so the only expected failure here is a duplicate
    column on an already-migrated database.
    """
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        first = stmt.strip().splitlines()[0][:120]
        raise SystemExit(
            "FATAL: a database schema migration failed and was NOT a benign "
            "already-applied case. Refusing to start so the app never trades on "
            f"an unknown schema.\n  statement: {first}\n  error: {e}"
        )


# The numbered steps live in steps.py (2026-09-10): the list had reached 661
# of this file's 800 lines, and the next migration would have crossed the LOC
# ceiling. Re-exported so every existing import path still resolves here.
from backend.migrations.steps import MIGRATIONS, _rename_gdc_column  # noqa: F401,E402


# The schema generation a fully migrated database carries = the last step.
SCHEMA_VERSION = MIGRATIONS[-1][0]


def _ensure_version_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version("
        "id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, applied_at REAL NOT NULL)"
    )


def _set_version(conn, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_version(id, version, applied_at) VALUES(1, ?, ?)",
        (version, time.time()),
    )


def run(conn) -> None:
    """Apply every step after the DB's recorded version, in order, advancing
    the stamp per step. Fail-closed: a failing step aborts with the stamp
    still pointing at the last good step."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    current = int(row["version"]) if row else 0
    for number, _title, step in MIGRATIONS:
        if number <= current:
            continue
        if callable(step):
            step(conn)
        else:
            for stmt in step:
                apply_migration(conn, stmt)
        _set_version(conn, number)


def stamp_schema_version(conn) -> None:
    """Record the head schema generation (kept for existing callers; run()
    normally stamps per step)."""
    _ensure_version_table(conn)
    _set_version(conn, SCHEMA_VERSION)


def verify_critical_schema(conn) -> None:
    """Abort if a money-critical table or column is missing after migration."""
    for table, cols in CRITICAL_SCHEMA.items():
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            raise SystemExit(
                f"FATAL: required table '{table}' is missing after schema "
                "migration — refusing to start on an incomplete schema."
            )
        missing = cols - present
        if missing:
            raise SystemExit(
                f"FATAL: table '{table}' is missing column(s) {sorted(missing)} "
                "after schema migration — refusing to start on an incomplete schema."
            )


def get_schema_version() -> int:
    """The recorded schema generation, or 0 if never stamped."""
    from backend.src.db.database import db  # lazy: avoid an import cycle
    with db() as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return int(row["version"]) if row else 0
