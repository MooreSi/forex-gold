"""SQLite implementation of DbAdapter, wrapping sqlite3.Connection -- see
docs/todo/refactor/backend-foundation/010-*.md.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .adapter import RunResult


class SqliteAdapter:
    def __init__(self, connection: sqlite3.Connection):
        self._con = connection
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        try:
            self._con.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # e.g. :memory: databases don't support WAL -- fine, no-op
        self._in_transaction = False

    def get(self, sql: str, *params: Any) -> sqlite3.Row | None:
        cur = self._con.execute(sql, params)
        return cur.fetchone()

    def all(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        cur = self._con.execute(sql, params)
        return cur.fetchall()

    def run(self, sql: str, *params: Any) -> RunResult:
        cur = self._con.execute(sql, params)
        if not self._in_transaction:
            self._con.commit()
        # cur.lastrowid is connection-level state in sqlite3: on a no-op
        # write (e.g. INSERT OR IGNORE hitting a UNIQUE conflict), it still
        # reports the PREVIOUS successful insert's rowid rather than None,
        # because this adapter reuses one persistent connection across every
        # call. Gate on rowcount so callers only see a lastrowid when this
        # specific statement actually wrote a row.
        lastrowid = cur.lastrowid if cur.rowcount > 0 else None
        return RunResult(lastrowid, cur.rowcount)

    def exec(self, sql: str) -> None:
        self._con.executescript(sql)
        if not self._in_transaction:
            self._con.commit()

    @contextmanager
    def transaction(self) -> Iterator["SqliteAdapter"]:
        if self._in_transaction:
            # Already inside an outer transaction -- participate without a
            # nested commit/rollback; the outermost block owns that.
            yield self
            return
        self._in_transaction = True
        try:
            yield self
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise
        finally:
            self._in_transaction = False

    def close(self) -> None:
        self._con.close()
