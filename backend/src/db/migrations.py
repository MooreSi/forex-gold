"""Schema migration mechanics — fail closed, and record what was applied.

Extracted from database.py (which is already oversized). The behaviour is the
review's data #2 fix: the old boot-time migrations ran ~90 ALTERs inside one
`except Exception: pass`, so a genuinely failed migration was indistinguishable
from an already-applied one and the app traded on an unknown schema.

These functions are pure (they take a connection) so they carry no import
dependency on database.py; database.py imports and calls them from _apply_schema.
"""
from __future__ import annotations

import sqlite3
import time

# Bump when the schema changes in a way worth recording. The migration list in
# database._apply_schema is idempotent (ADD COLUMN / CREATE TABLE IF NOT EXISTS),
# so this is an observability + pre-flight anchor, not a per-step counter.
SCHEMA_VERSION = 1

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


def stamp_schema_version(conn) -> None:
    """Record the current schema generation (single-row table)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version("
        "id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, applied_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version(id, version, applied_at) VALUES(1, ?, ?)",
        (SCHEMA_VERSION, time.time()),
    )


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
