"""1 pip = 0.10 price for this app's XAUUSD feed, and the one-off migration
that keeps every pre-existing EA Template trading the same real distance
after the Anchor TP ladder's pips-to-price conversion was fixed.

Root cause (2026-07-31, ticket 1689710560): core_open_trade.py's
_pips_ladder added a template's tp{n}_pips straight into price with no
conversion, so a "30 pips" entry moved the level 30.0 points (300 pips) --
10x too far. ForexTraderBridge.mq5's PipsToPrice() already did this
correctly for the Pending ladder and for trailing distances; only the
Python-computed Anchor ladder (and its sibling, the pending-pips-from-a-
Telegram-price conversion a few lines below it) had the bug.
"""
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD, pips_to_price
from tests.conftest import remove_db_file


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db_path():
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    remove_db_file(path)


def test_conversion_matches_the_channels_own_wording():
    """"TP1 HIT +30 PIPS (4076 TO 4079)" -- 3.0 of price for 30 stated pips,
    the ratio confirmed across two dozen real messages 2026-07-31."""
    assert pips_to_price(30) == pytest.approx(3.0)
    assert PIPS_TO_PRICE_XAUUSD == pytest.approx(0.10)


def test_zero_and_negative_pips():
    assert pips_to_price(0) == 0.0
    assert pips_to_price(-30) == pytest.approx(-3.0)


# ── Migration: existing templates must trade the same real distance ────────

def test_migration_scales_every_nonzero_anchor_level(fresh_db_path):
    import sqlite3
    con = sqlite3.connect(fresh_db_path)
    con.execute(
        "CREATE TABLE ea_trade_templates (name TEXT PRIMARY KEY, "
        + ",".join(f"tp{n}_pips REAL DEFAULT 0.0" for n in range(1, 9))
        + ")"
    )
    con.execute(
        "INSERT INTO ea_trade_templates (name, tp1_pips, tp2_pips, tp3_pips) "
        "VALUES ('GD Instituational - single', 3.0, 7.0, 12.0)"
    )
    con.commit()
    con.close()

    db.init(fresh_db_path)  # runs _apply_schema -> the one-off migration

    con = sqlite3.connect(fresh_db_path)
    row = con.execute(
        "SELECT tp1_pips, tp2_pips, tp3_pips FROM ea_trade_templates "
        "WHERE name='GD Instituational - single'"
    ).fetchone()
    assert row == (30.0, 70.0, 120.0)


def test_migration_leaves_zero_levels_at_zero(fresh_db_path):
    """A template with an unused TP slot must not have 0 * 10 silently
    become nonzero, or a level the template never configured would suddenly
    activate."""
    import sqlite3
    con = sqlite3.connect(fresh_db_path)
    con.execute(
        "CREATE TABLE ea_trade_templates (name TEXT PRIMARY KEY, "
        + ",".join(f"tp{n}_pips REAL DEFAULT 0.0" for n in range(1, 9))
        + ")"
    )
    con.execute(
        "INSERT INTO ea_trade_templates (name, tp1_pips) VALUES ('Sparse', 5.0)"
    )
    con.commit()
    con.close()

    db.init(fresh_db_path)

    con = sqlite3.connect(fresh_db_path)
    row = con.execute(
        "SELECT tp1_pips, tp2_pips, tp8_pips FROM ea_trade_templates WHERE name='Sparse'"
    ).fetchone()
    assert row == (50.0, 0.0, 0.0)


def test_migration_runs_exactly_once(fresh_db_path):
    """Running _apply_schema again (every app startup does) must not
    re-multiply an already-migrated value."""
    import sqlite3
    con = sqlite3.connect(fresh_db_path)
    con.execute(
        "CREATE TABLE ea_trade_templates (name TEXT PRIMARY KEY, "
        + ",".join(f"tp{n}_pips REAL DEFAULT 0.0" for n in range(1, 9))
        + ")"
    )
    con.execute("INSERT INTO ea_trade_templates (name, tp1_pips) VALUES ('T', 5.0)")
    con.commit()
    con.close()

    db.init(fresh_db_path)
    db._apply_schema()
    db._apply_schema()

    con = sqlite3.connect(fresh_db_path)
    row = con.execute("SELECT tp1_pips FROM ea_trade_templates WHERE name='T'").fetchone()
    assert row == (50.0,)


def test_a_template_created_after_the_fix_is_not_migrated(fresh_db_path):
    """The marker must be global, not per-row: a template someone creates
    after this fix ships enters true pips from the start and must not be
    treated as pre-fix legacy data on its very first save."""
    from backend.src.services.broker import ea_templates as et

    db.init(fresh_db_path)  # migration marker set here, on an empty table
    et.save_ea_template("New Template", {"tp1_pips": 30.0})

    import sqlite3
    con = sqlite3.connect(fresh_db_path)
    row = con.execute(
        "SELECT tp1_pips FROM ea_trade_templates WHERE name='New Template'"
    ).fetchone()
    assert row == (30.0,)  # unchanged -- not multiplied
