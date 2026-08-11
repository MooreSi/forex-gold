"""Legacy-DB upgrade proof (stage2 phase2/020).

The risk is not the fresh DB — it's a real *installed* database created by
an older schema generation upgrading in place. The base DDL in
db/schema_sql.py deliberately lacks every column the migration registry
adds (managed_by, order_type, tp6-8, …), so executing it alone produces a
genuine historical shape: exactly what an old install looks like before
boot-time migrations run.

Three shapes are built with raw sqlite3 (no db.init, no fixtures), then
upgraded via migrations.run + backfills.run and checked against head.
Nothing here can reach a broker.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.db import backfills, migrations
from backend.src.db.schema_sql import SCHEMA


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _base_only(path) -> sqlite3.Connection:
    """Shape A: base DDL, no migrations ever ran, no version stamp."""
    conn = _connect(path)
    conn.executescript(SCHEMA)
    return conn


def _stopped_at(path, version: int) -> sqlite3.Connection:
    """Shape B/C: an install whose last boot ran steps 1..version."""
    conn = _connect(path)
    conn.executescript(SCHEMA)
    migrations._ensure_version_table(conn)
    for number, _title, step in migrations.MIGRATIONS:
        if number > version:
            break
        if callable(step):
            step(conn)
        else:
            for stmt in step:
                migrations.apply_migration(conn, stmt)
        migrations._set_version(conn, number)
    return conn


def test_legacy_shapes_reach_head(tmp_path):
    shapes = {
        "base-only": _base_only(tmp_path / "a.db"),
        "stopped-at-5": _stopped_at(tmp_path / "b.db", 5),
        "stopped-at-10": _stopped_at(tmp_path / "c.db", 10),
    }
    for name, conn in shapes.items():
        migrations.run(conn)
        backfills.run(conn)
        migrations.verify_critical_schema(conn)  # must not raise
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        assert int(row["version"]) == migrations.SCHEMA_VERSION, name
        assert {"tp1_pips", "tp8_pct"} <= _cols(conn, "ea_trade_templates"), name
        conn.close()


def test_upgrade_is_lossless(tmp_path):
    """Rows written under the old shape survive the upgrade byte-for-byte in
    every pre-existing column, and the new columns take their defaults."""
    conn = _base_only(tmp_path / "lossless.db")
    conn.execute(
        "INSERT INTO vantage_signals(signal_id, source_name, direction, entry_low,"
        " entry_high, stop_loss, status, created_at) VALUES"
        " ('sig-1', 'GoldChannel', 'BUY', 2400.0, 2401.0, 2390.0, 'pending', 1000.0)"
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades(trade_id, signal_id, direction, entry_low,"
        " entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time,"
        " strategy) VALUES ('tr-1', 'sig-1', 'BUY', 2400.0, 2401.0, 2400.5, 0.05, 0.05,"
        " 2390.0, 'open', 1000.0, 'scale_out')"
    )
    conn.commit()

    migrations.run(conn)
    backfills.run(conn)

    sig = conn.execute("SELECT * FROM vantage_signals WHERE signal_id='sig-1'").fetchone()
    assert sig["source_name"] == "GoldChannel"
    assert sig["entry_low"] == 2400.0 and sig["stop_loss"] == 2390.0
    assert sig["created_at"] == 1000.0
    assert sig["tp8"] is None  # new column, default

    trade = conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id='tr-1'").fetchone()
    assert trade["lot_size"] == 0.05 and trade["remaining_lots"] == 0.05
    assert trade["strategy"] == "scale_out"
    assert trade["managed_by"] == "python"   # new column takes its default
    assert trade["order_type"] == "market"   # new column takes its default
    assert conn.execute("SELECT COUNT(*) c FROM vantage_simulated_trades").fetchone()["c"] == 1
    conn.close()


def test_detector_can_fail(tmp_path):
    """Negative control: before the registry runs, the base shape really is
    missing the money-critical columns and the pre-flight refuses it."""
    conn = _base_only(tmp_path / "unmigrated.db")
    assert "managed_by" not in _cols(conn, "vantage_simulated_trades")
    with pytest.raises(SystemExit):
        migrations.verify_critical_schema(conn)
    conn.close()
