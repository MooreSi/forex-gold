"""The ordered, numbered migration registry (stage2 phase2/010).

The ~90 idempotent ALTER/CREATE statements used to live inline in
database._apply_schema as one flat loop with a single version stamp. The
registry gives them per-step numbers applied in order, so a DB records
which migrations it has and a new one can only be appended, never
interleaved. Fail-closed behaviour (skip duplicate-column, abort on a
real error) is inherited from apply_migration and pinned by
tests/db/test_migrations.py.

Uses fresh_db from tests/conftest.py. Nothing here can reach a broker.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.db import database as db
from backend.migrations import registry as migrations


def test_registry_is_ordered_and_dense():
    """Steps are numbered 1..head with no gaps or duplicates — the property
    that makes "which migrations does this DB have" a single integer."""
    numbers = [num for num, _title, _step in migrations.MIGRATIONS]
    assert numbers == list(range(1, len(numbers) + 1))
    assert migrations.SCHEMA_VERSION == numbers[-1]
    for _num, title, step in migrations.MIGRATIONS:
        assert title.strip()
        assert callable(step) or (isinstance(step, list) and step)


def test_steps_apply_in_order_and_advance_version(fresh_db):
    """A fresh DB reaches head, and the stamp records the head step."""
    assert db.get_schema_version() == migrations.SCHEMA_VERSION


def test_rerun_is_idempotent(fresh_db):
    """Running the registry again applies nothing and changes nothing."""
    with fresh_db.db() as conn:
        before = {r["name"] for r in conn.execute("PRAGMA table_info(vantage_risk_settings)")}
        migrations.run(conn)
        after = {r["name"] for r in conn.execute("PRAGMA table_info(vantage_risk_settings)")}
    assert after == before
    assert db.get_schema_version() == migrations.SCHEMA_VERSION


def test_registry_matches_schema(fresh_db):
    """Parity with the old flat loop: the columns/tables it created are all
    present after the registry runs, and the critical pre-flight passes."""
    with fresh_db.db() as conn:
        migrations.verify_critical_schema(conn)  # must not raise

        def cols(table: str) -> set:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

        # spot-checks across the historical waves, first to last
        assert "group_name" in cols("vantage_tg_signals")
        assert {"tp6", "tp7", "tp8"} <= cols("vantage_signals")
        assert "ooh_strategy" in cols("vantage_risk_settings")
        assert "managed_by" in cols("vantage_simulated_trades")
        assert "strategy" in cols("vantage_pending_orders")
        assert "lk_entry_realignment" in cols("vantage_risk_settings")
        assert {"tp1_pips", "tp8_pips", "tp1_pct", "tp8_pct"} <= cols("ea_trade_templates")
        assert cols("channel_strategy_rec"), "channel_strategy_rec table missing"
        assert cols("logic_keyword_lexicons"), "logic_keyword_lexicons table missing"


def test_partially_migrated_db_resumes_from_its_version(fresh_db):
    """A DB stamped at step N runs only the steps after N (idempotency makes
    re-running old steps safe, but the version is the record)."""
    with fresh_db.db() as conn:
        # Pretend this DB stopped at step 1.
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(id, version, applied_at) VALUES(1, 1, 0)"
        )
        migrations.run(conn)
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    assert int(row["version"]) == migrations.SCHEMA_VERSION


def test_bad_step_aborts_and_version_stops_before_it(fresh_db, monkeypatch):
    """Fail closed: a genuinely failing step aborts startup, and the stamp
    proves nothing after it ran (negative control for the whole runner)."""
    bad = migrations.SCHEMA_VERSION + 1
    monkeypatch.setattr(
        migrations, "MIGRATIONS",
        migrations.MIGRATIONS + [(bad, "planted failure",
                                  ["ALTER TABLE no_such_table ADD COLUMN x INTEGER"])],
    )
    with fresh_db.db() as conn:
        with pytest.raises(SystemExit):
            migrations.run(conn)
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    assert int(row["version"]) == migrations.SCHEMA_VERSION  # not the planted step
