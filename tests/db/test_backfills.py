"""Named, explicit data backfills (stage2 phase2/030).

The one-off data corrections (2026-07-23 rebrand renames, instant:-prefix
stripping, GD2 instant-entry enable, order_type backfill, DPM tg_source
backfill) used to live inline in database._apply_schema, several wrapped
in their own `except Exception: pass`. They are now named steps in
db/backfills.py with one explicit policy: a missing table/column is benign
(skipped), anything else aborts startup.

Uses fresh_db from tests/conftest.py. Nothing here can reach a broker.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.db import backfills
from backend.src.db import database as db


def _seed_legacy_rows(conn) -> None:
    """Rows shaped like the pre-2026-07-23 data each backfill corrects."""
    conn.execute(
        "INSERT INTO vantage_signals(signal_id, source_name, direction, entry_low, entry_high,"
        " stop_loss, status, created_at) VALUES ('sig-legacy', 'instant:GoldChannel', 'BUY',"
        " 2400.0, 2401.0, 2390.0, 'pending', 0)"
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades(trade_id, signal_id, direction, entry_low,"
        " entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time,"
        " strategy, tg_source, order_type) VALUES ('tr-legacy', 'sig-legacy', 'BUY', 2400.0,"
        " 2401.0, 2400.5, 0.05, 0.05, 2390.0, 'open', 0, 'limit_runner', 'GD Copy Engine',"
        " 'market')"
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades(trade_id, signal_id, direction, entry_low,"
        " entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time,"
        " strategy, tg_source, order_type) VALUES ('tr-gdvip', 'sig-legacy', 'BUY', 2400.0,"
        " 2401.0, 2400.5, 0.05, 0.05, 2390.0, 'open', 0, 'gd_vip_runner', 'instant:OldChan',"
        " 'market')"
    )
    conn.execute(
        "INSERT INTO channel_parser_config(channel_name, parser_format, instant_entry_enabled)"
        " VALUES ('GD2 VIP', 'gd2', 0)"
    )
    conn.execute(
        "INSERT INTO dpm_trade_performance(trade_id, tg_source) VALUES ('tr-legacy', NULL)"
    )


def test_each_backfill_applies_on_legacy_data(fresh_db):
    with fresh_db.db() as conn:
        _seed_legacy_rows(conn)
        backfills.run(conn)

        trade = conn.execute(
            "SELECT strategy, tg_source, order_type FROM vantage_simulated_trades"
            " WHERE trade_id='tr-legacy'"
        ).fetchone()
        assert trade["tg_source"] == "Reversal Engine"       # rebrand: source string
        assert trade["order_type"] == "limit"                # order_type backfill

        gdvip = conn.execute(
            "SELECT strategy, tg_source, order_type FROM vantage_simulated_trades"
            " WHERE trade_id='tr-gdvip'"
        ).fetchone()
        assert gdvip["strategy"] == "reversal_runner"        # rebrand: strategy id
        assert gdvip["tg_source"] == "OldChan"               # instant: prefix stripped
        assert gdvip["order_type"] == "market"               # untouched (not a runner)

        sig = conn.execute(
            "SELECT source_name FROM vantage_signals WHERE signal_id='sig-legacy'"
        ).fetchone()
        assert sig["source_name"] == "GoldChannel"           # instant: prefix stripped

        cfg = conn.execute(
            "SELECT instant_entry_enabled FROM channel_parser_config"
            " WHERE channel_name='GD2 VIP'"
        ).fetchone()
        assert cfg["instant_entry_enabled"] == 1             # GD2 IME enable

        dpm = conn.execute(
            "SELECT tg_source FROM dpm_trade_performance WHERE trade_id='tr-legacy'"
        ).fetchone()
        assert dpm["tg_source"] == "Reversal Engine"         # backfilled from the trade


def test_backfills_are_idempotent(fresh_db):
    """A second run changes nothing — same rows, byte for byte."""
    with fresh_db.db() as conn:
        _seed_legacy_rows(conn)
        backfills.run(conn)
        first = [dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades ORDER BY trade_id")]
        backfills.run(conn)
        second = [dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades ORDER BY trade_id")]
    assert second == first


def test_missing_table_is_benign(fresh_db):
    """The one benign case: the table/column isn't on this schema yet."""
    with fresh_db.db() as conn:
        # must not raise
        backfills.execute_tolerant(conn, "UPDATE no_such_table SET x=1", "probe")


def test_backfill_failure_is_not_swallowed(fresh_db):
    """Negative control: a real failure (not missing-schema) aborts loudly —
    the exact opposite of the old except-pass."""
    with fresh_db.db() as conn:
        with pytest.raises(SystemExit):
            backfills.execute_tolerant(conn, "UPDATE vantage_signals SET", "planted")


def test_apply_schema_has_no_silent_excepts():
    """The schema/backfill path carries no `except Exception: pass` any more."""
    src = inspect.getsource(db._apply_schema)
    assert "except Exception" not in src
    assert "pass" not in src
