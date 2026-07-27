"""Regression test for a real bug found 2026-07-21: switching accounts via
init() (used by the demo/live toggle in ui/app.py's _do_env_switch) did not
redirect already-cached per-thread db() connections to the new path -- any
thread that had already opened a connection (the calling/main thread, and
the single to_db_thread() worker) kept silently reading/writing the OLD
database file. This meant _apply_schema() itself could reuse a stale
connection and never actually touch the new file, leaving it schema-less
until the next full process restart -- and any code that opens a fresh
connection against the new path (e.g. reversal_engine_correlate.py's VIP
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


def test_init_invalidates_the_risk_settings_cache():
    """The same 2026-07-21 bug, one layer up, found 2026-07-25.

    get_risk_settings() memoises for _RS_CACHE_TTL (10s) keyed on nothing but
    time. init() closed the stale *connections* but left that cache populated,
    so for ten seconds after a demo/live switch the app kept answering with the
    other environment's risk settings -- the session gates and the Max Risk per
    trade % ceiling among them.

    It was also what made the test suite flaky: every fresh_db builds a new temp
    database, so any test that only read settings within ten seconds of another
    silently got the previous test's values from a deleted file.
    """
    fd1, path_a = tempfile.mkstemp(suffix=".db")
    os.close(fd1); os.remove(path_a)
    fd2, path_b = tempfile.mkstemp(suffix=".db")
    os.close(fd2); os.remove(path_b)
    try:
        db.init(path_a)
        db.update_risk_settings({"max_risk_per_trade_pct": 5.0})
        assert db.get_risk_settings()["max_risk_per_trade_pct"] == 5.0
        assert db._rs_cache is not None, "precondition: the read populated the cache"

        # Switching environments immediately, well inside the 10s TTL.
        db.init(path_b)
        assert db._rs_cache is None, "init() must invalidate the settings cache"

        # The fresh database must answer with its own default, not 5.0.
        assert db.get_risk_settings()["max_risk_per_trade_pct"] == 1.0
    finally:
        for p in (path_a, path_b):
            if os.path.exists(p):
                os.remove(p)
