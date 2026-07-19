"""Characterizes the required behavior of DbAdapter/SqliteAdapter before
anything depends on it — see docs/todo/refactor/backend-foundation/010-*.md.
"""
import os
import sqlite3
import tempfile

import pytest

from forex_trader.src.db.sqlite_adapter import SqliteAdapter


@pytest.fixture
def adapter():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    a = SqliteAdapter(con)
    a.exec("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT, amount REAL)")
    yield a
    a.close()
    os.remove(path)


def test_run_returns_lastrowid_and_get_reads_it_back(adapter):
    result = adapter.run("INSERT INTO t (val, amount) VALUES (?, ?)", "hello", 1.5)
    assert result.lastrowid == 1
    row = adapter.get("SELECT * FROM t WHERE id=?", 1)
    assert row["val"] == "hello"
    assert row["amount"] == 1.5


def test_all_returns_every_matching_row_in_order(adapter):
    adapter.run("INSERT INTO t (val) VALUES (?)", "a")
    adapter.run("INSERT INTO t (val) VALUES (?)", "b")
    rows = adapter.all("SELECT * FROM t ORDER BY id")
    assert [r["val"] for r in rows] == ["a", "b"]


def test_get_returns_none_when_no_row_matches(adapter):
    assert adapter.get("SELECT * FROM t WHERE id=?", 999) is None


def test_run_lastrowid_is_none_on_a_no_op_insert(adapter):
    """Regression test: found via gd_copy_signal's characterization suite.
    sqlite3's cursor.lastrowid is connection-level state -- since this
    adapter reuses one persistent connection, an INSERT OR IGNORE that hits
    a UNIQUE conflict (writes nothing) would otherwise still report the
    PREVIOUS successful insert's rowid instead of "no row was written"."""
    adapter.exec("CREATE UNIQUE INDEX idx_t_val ON t(val)")
    first = adapter.run("INSERT OR IGNORE INTO t (val) VALUES (?)", "unique-val")
    assert first.lastrowid is not None
    second = adapter.run("INSERT OR IGNORE INTO t (val) VALUES (?)", "unique-val")
    assert second.lastrowid is None
    assert second.rowcount == 0


def test_transaction_commits_all_statements_together(adapter):
    with adapter.transaction() as tx:
        tx.run("INSERT INTO t (val) VALUES (?)", "x")
        tx.run("INSERT INTO t (val) VALUES (?)", "y")
    rows = adapter.all("SELECT * FROM t")
    assert len(rows) == 2


def test_transaction_rolls_back_all_statements_on_exception(adapter):
    """The killer test: this is the exact bug class close_signal() has today
    (database.py:332-352 does the status update and balance update as two
    separate, independently-committed connections). A transaction() block
    must roll back BOTH statements together, not leave the first one
    committed while the second never happens."""
    with pytest.raises(RuntimeError):
        with adapter.transaction() as tx:
            tx.run("INSERT INTO t (val) VALUES (?)", "x")
            tx.run("INSERT INTO t (val) VALUES (?)", "y")
            raise RuntimeError("simulated crash mid-transaction")
    rows = adapter.all("SELECT * FROM t")
    assert len(rows) == 0, "both inserts should have rolled back together"


def test_without_transaction_each_run_commits_independently(adapter):
    """Contrast case: this is the CURRENT (buggy-for-multi-step-writes)
    behavior transaction() exists to replace for multi-statement sequences.
    Plain run() calls outside a transaction still commit individually --
    correct for genuinely single-statement writes, wrong when two writes
    must succeed or fail as a unit."""
    adapter.run("INSERT INTO t (val) VALUES (?)", "first")
    try:
        adapter.run("INSERT INTO t (val) VALUES (?)", "second")
        raise RuntimeError("simulated crash after both statements committed")
    except RuntimeError:
        pass
    rows = adapter.all("SELECT * FROM t")
    assert len(rows) == 2, "both survived independently -- this is the gap transaction() closes"
