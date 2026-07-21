"""Regression test for a real bug found 2026-07-21: switching accounts via
init() (used by the demo/live toggle in ui/app.py's _do_env_switch) did not
redirect already-cached per-thread db() connections to the new path -- any
thread that had already opened a connection (the calling/main thread, and
the single to_db_thread() worker) kept silently reading/writing the OLD
database file. This meant _apply_schema() itself could reuse a stale
connection and never actually touch the new file, leaving it schema-less
until the next full process restart -- and any code that opens a fresh
connection against the new path (e.g. gd_copy_signal_correlate.py's VIP
fetch) broke immediately with "no such table".
"""
import asyncio
import os
import tempfile

import pytest

from forex_trader.core import database as db


def test_init_redirects_the_calling_threads_cached_connection():
    fd1, path_a = tempfile.mkstemp(suffix=".db")
    os.close(fd1)
    os.remove(path_a)
    fd2, path_b = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    os.remove(path_b)
    try:
        db.init(path_a)
        with db.db() as conn:
            conn.execute("SELECT 1")  # caches a connection on this thread

        db.init(path_b)

        with db.db() as conn:
            actual_path = conn.execute("PRAGMA database_list").fetchall()[0][2]
        assert os.path.realpath(actual_path) == os.path.realpath(path_b)

        # And the new file must actually have the schema applied -- not just
        # be a bare file created by mkdir/lazy-open.
        with db.db() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert len(tables) > 20  # full schema has ~30 tables
    finally:
        db._close_thread_local_conn()
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.remove(p)


def test_init_redirects_the_db_worker_threads_cached_connection():
    fd1, path_a = tempfile.mkstemp(suffix=".db")
    os.close(fd1)
    os.remove(path_a)
    fd2, path_b = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    os.remove(path_b)

    async def _run():
        db.init(path_a)

        def touch():
            with db.db() as conn:
                return conn.execute("PRAGMA database_list").fetchall()[0][2]

        await db.to_db_thread(touch)  # caches a connection on the db-worker thread
        db.init(path_b)
        worker_path = await db.to_db_thread(touch)
        assert os.path.realpath(worker_path) == os.path.realpath(path_b)

    try:
        asyncio.run(_run())
    finally:
        db._close_thread_local_conn()
        db._db_executor.submit(db._close_thread_local_conn).result(timeout=5)
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.remove(p)
