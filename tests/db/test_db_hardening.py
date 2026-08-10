"""Connection hardening + a real backup for the live-money books.

Review data H4 / backend H8: the trading DB connection set no busy_timeout, so
under the two writer threads a concurrent write errors immediately with
"database is locked"; and there was no backup of any kind — the books were one
SQLite file on one disk.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from backend.src.db import backup as bk
from backend.src.db import database as db


def test_connection_sets_a_busy_timeout(fresh_db):
    with fresh_db.db() as conn:
        (val,) = conn.execute("PRAGMA busy_timeout").fetchone()
    assert val >= 1000  # non-zero -> SQLite waits for the lock instead of erroring


def _seed_db(path):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE t(x INTEGER)")
    c.execute("INSERT INTO t VALUES (42)")
    c.commit()
    c.close()


def test_backup_now_creates_a_valid_copy(tmp_path):
    src = tmp_path / "live.db"
    _seed_db(src)
    dest = bk.backup_now(src, tmp_path / "backups")
    assert dest.exists()
    c = sqlite3.connect(str(dest))
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT x FROM t").fetchone()[0] == 42  # data actually copied
    c.close()


def test_rotate_keeps_the_n_newest(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    made = []
    for i in range(5):
        p = d / f"backup_2026010{i}_000000.db"
        p.write_text("x")
        os.utime(p, (1000 + i, 1000 + i))  # strictly increasing mtime
        made.append(p)
    removed = bk.rotate(d, keep=3)
    remaining = set(d.glob("backup_*.db"))
    assert len(remaining) == 3
    assert made[0] in removed and made[1] in removed  # two oldest gone
    assert made[4] in remaining                        # newest kept


def test_rotate_is_a_noop_under_the_limit(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "backup_20260101_000000.db").write_text("x")
    assert bk.rotate(d, keep=30) == []  # negative control: nothing removed


def test_maybe_daily_takes_one_then_skips_same_day(tmp_path):
    src = tmp_path / "live.db"
    _seed_db(src)
    d = tmp_path / "backups"
    first = bk.maybe_daily_backup(src, d)
    assert first is not None and first.exists()
    second = bk.maybe_daily_backup(src, d)
    assert second is None  # today's already exists
    assert len(list(d.glob("backup_*.db"))) == 1
