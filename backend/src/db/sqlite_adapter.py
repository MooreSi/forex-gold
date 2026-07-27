"""SQLite implementation of DbAdapter, wrapping sqlite3.Connection -- see
docs/todo/refactor/backend-foundation/010-*.md.
"""
from __future__ import annotations

import sqlite3
import threading
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
        # This adapter deliberately reuses one persistent connection across
        # every call (see run()'s own comment) rather than opening a fresh
        # connection per query -- but the app dispatches DB calls from more
        # than one thread onto that same connection (core.database.to_db_thread's
        # dedicated worker thread for most UI reads, plus direct synchronous
        # calls from each engine's own async loop, which runs on the main
        # thread). sqlite3.Connection is not safe for concurrent use across
        # threads even with check_same_thread=False (that flag only lifts the
        # same-thread assertion, it doesn't add internal locking) -- confirmed
        # live 2026-07-21: wiring breakout_signal + test_signal alongside
        # reversal_engine surfaced "SQLite objects created in a thread can only
        # be used in that same thread" the moment more than one engine's
        # to_db_thread-wrapped UI panel and main-thread engine loop were both
        # hitting their own adapter. An RLock (not a plain Lock) so
        # transaction()'s nested run() calls from the same thread don't
        # deadlock against the outer lock it already holds.
        self._lock = threading.RLock()

    def get(self, sql: str, *params: Any) -> sqlite3.Row | None:
        with self._lock:
            cur = self._con.execute(sql, params)
            return cur.fetchone()

    def all(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._con.execute(sql, params)
            return cur.fetchall()

    def run(self, sql: str, *params: Any) -> RunResult:
        with self._lock:
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
        with self._lock:
            self._con.executescript(sql)
            if not self._in_transaction:
                self._con.commit()

    @contextmanager
    def transaction(self) -> Iterator["SqliteAdapter"]:
        with self._lock:
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
        with self._lock:
            self._con.close()
