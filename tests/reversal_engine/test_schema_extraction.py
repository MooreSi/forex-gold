"""The reversal engine's tables are created exactly as before.

`reversal_engine_repo.py` sat 42 lines over the 800-line ceiling, and 127 of
those lines were one function: `_create_schema`, a single DDL blob with one
caller and one dependency.

Moving DDL is the kind of change where "it still runs" proves very little --
a dropped CREATE TABLE only surfaces the first time something writes to the
missing table, which for several of these is days later. So this pins the
tables and their columns, not the fact that the function returns.

The list below was taken from the schema as it stood before the move.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as repo

# Every table the reversal engine's own database carries.
EXPECTED_TABLES = {
    "re_signals", "re_levels", "re_correlation", "re_analysis_log",
    "re_config", "re_balance_log", "re_near_miss", "re_daily_research",
}


@pytest.fixture
def re_db(tmp_path):
    repo.init(str(tmp_path / "reversal.db"))
    return repo


def _tables(r) -> set[str]:
    return {row["name"] for row in r.get_db().all(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(r, table) -> set[str]:
    return {row[1] for row in r.get_db().all(f"PRAGMA table_info({table})")}


def test_every_table_is_created(re_db):
    missing = EXPECTED_TABLES - _tables(re_db)
    assert missing == set(), f"the schema no longer creates: {sorted(missing)}"


def test_the_signals_table_keeps_its_shape(re_db):
    """re_signals is the one every other part of the engine writes through."""
    cols = _columns(re_db, "re_signals")
    for required in ("signal_ref", "direction", "entry_low", "entry_high",
                     "stop_loss", "sl_dist", "created_at"):
        assert required in cols, f"re_signals lost {required}"


def test_the_tp_ladder_keeps_its_depth(re_db):
    """Eight, not the ten the anchor ladder carries elsewhere. Asserted as it
    is rather than as I first assumed -- a characterization test that argues
    with the schema is just a wrong test."""
    cols = _columns(re_db, "re_signals")
    for n in range(1, 9):
        assert f"tp{n}" in cols, f"re_signals lost tp{n}"
    assert "tp9" not in cols, "the ladder grew; update this test deliberately"


def test_creating_the_schema_twice_is_harmless(re_db, tmp_path):
    """init() runs on every startup, so the DDL has to be idempotent."""
    before = _tables(re_db)
    repo.init(str(tmp_path / "reversal.db"))
    assert _tables(repo) == before


def test_the_schema_check_would_notice_a_dropped_table():
    """Negative control: an empty database must not satisfy the assertion
    above, or none of this proves anything."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        import sqlite3
        conn = sqlite3.connect(path)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert EXPECTED_TABLES - names == EXPECTED_TABLES
    finally:
        os.remove(path)
