"""Schema migrations must fail closed, not silently swallow real errors.

Review 2026-08-08, data #2: `_apply_schema` ran ~90 ALTERs inside one
`except Exception: pass`, so a genuinely failed migration was indistinguishable
from an already-applied one and the app traded on an unknown schema.

The fix: a benign already-applied migration (duplicate column) is skipped; any
other failure aborts startup. A `schema_version` row is stamped, and a
post-migration check verifies the money-critical shape actually applied. These
tests pin all of that, each detection with a negative control.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.db import database as db


def test_fresh_db_stamps_schema_version(fresh_db):
    assert fresh_db.get_schema_version() == db.SCHEMA_VERSION


def test_fresh_db_has_money_critical_columns(fresh_db):
    with fresh_db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(vantage_simulated_trades)")}
    # columns added by the ADD COLUMN migrations — proof the loop ran
    assert {"trade_id", "managed_by", "order_type"} <= cols


def test_reinit_on_existing_db_does_not_abort(tmp_path):
    """The real-world reboot: every ALTER hits 'duplicate column' and is skipped.

    This is the regression that would bite hardest — if the tightened error
    handling treated 'already applied' as fatal, every normal restart would
    crash. Run init twice on the same file; it must succeed both times.
    """
    path = str(tmp_path / "reinit.db")
    db.init(path)
    first = db.get_schema_version()
    db.init(path)  # must not raise — all migrations are duplicate-column now
    assert db.get_schema_version() == first == db.SCHEMA_VERSION


def test_migration_adds_a_missing_column(fresh_db):
    """The upgrade path: a column absent from an existing table gets added."""
    with fresh_db.db() as conn:
        conn.execute("CREATE TABLE _probe (id INTEGER)")
        db._apply_migration(conn, "ALTER TABLE _probe ADD COLUMN newcol INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(_probe)")}
    assert "newcol" in cols


def test_apply_migration_skips_already_applied(fresh_db):
    """A duplicate-column migration is benign and must not raise."""
    with fresh_db.db() as conn:
        conn.execute("CREATE TABLE _probe2 (id INTEGER, newcol INTEGER)")
        # newcol already exists → 'duplicate column name' → skipped, no raise
        db._apply_migration(conn, "ALTER TABLE _probe2 ADD COLUMN newcol INTEGER")


def test_apply_migration_aborts_on_a_real_error(fresh_db):
    """The killer test / negative control: a NON-benign failure must abort.

    'no such table' is not 'already applied' — the old code swallowed it; the
    new code must refuse to continue.
    """
    with fresh_db.db() as conn:
        with pytest.raises(SystemExit):
            db._apply_migration(conn, "ALTER TABLE table_that_does_not_exist ADD COLUMN x INTEGER")


def test_verify_critical_schema_passes_on_fresh_db(fresh_db):
    with fresh_db.db() as conn:
        db._verify_critical_schema(conn)  # must not raise


def test_verify_critical_schema_detects_a_missing_column(tmp_path):
    """Negative control for the pre-flight: a missing required column aborts."""
    con = sqlite3.connect(str(tmp_path / "broken.db"))
    con.row_factory = sqlite3.Row
    # required tables present, but vantage_simulated_trades is missing managed_by
    con.execute("CREATE TABLE vantage_simulated_trades (trade_id TEXT, order_type TEXT)")
    con.execute("CREATE TABLE vantage_risk_settings (circuit_breaker_enabled INTEGER, re_live_execution INTEGER)")
    con.execute("CREATE TABLE vantage_signals (signal_id TEXT, status TEXT)")
    with pytest.raises(SystemExit):
        db._verify_critical_schema(con)
    con.close()
