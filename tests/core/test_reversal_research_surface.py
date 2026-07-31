"""Proves backend.src.services.reversal_engine.research's extracted
reversal_engine_research_sweep behaves identically to SimulationEngine's original,
characterized in test_reversal_engine_research_characterization.py -- see
docs/todo/refactor/core-reversal-research-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class
(now/research_runner passed explicitly instead of mocking engine.datetime).
No real or demo MT5 order is ever placed, closed, or modified -- this
module's function never calls an order-placing collaborator at all.
"""
import asyncio
import os
import tempfile
from datetime import datetime

import pytest
from unittest import mock

from forex_trader.core import database as db
from backend.src.services.reversal_engine import research as re_research


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def test_not_2200_no_pipeline_call_no_dedup_write(fresh_db):
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 21, 59, 0), research_runner=runner))
    assert calls == []
    assert db.get_app_config("re_research_last") is None


def test_remote_node_skips_even_at_2200(fresh_db):
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    with mock.patch.object(db, "is_remote_node", return_value=True):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 22, 0, 0), research_runner=runner))
    assert calls == []


def test_already_ran_today_skips(fresh_db):
    db.set_app_config("re_research_last", "2026-07-20")
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 22, 0, 0), research_runner=runner))
    assert calls == []


def test_runs_with_engine_and_marks_dedup_when_ran_true(fresh_db):
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    sentinel = object()
    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            sentinel, now=datetime(2026, 7, 20, 22, 0, 0), research_runner=runner))
    assert calls == [sentinel]
    assert db.get_app_config("re_research_last") == "2026-07-20"


def test_ran_false_does_not_mark_dedup(fresh_db):
    async def runner(engine):
        return {"ran": False}

    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 22, 0, 0), research_runner=runner))
    assert db.get_app_config("re_research_last") is None


def test_pipeline_exception_propagates(fresh_db):
    """The extracted function no longer has the loop's own outer
    try/except -- that stays in engine.py's thin wrapper. An exception
    from the pipeline propagates to the caller here."""
    async def runner(engine):
        raise RuntimeError("pipeline boom")

    with mock.patch.object(db, "is_remote_node", return_value=False):
        with pytest.raises(RuntimeError, match="pipeline boom"):
            asyncio.run(re_research.reversal_engine_research_sweep(
                "engine", now=datetime(2026, 7, 20, 22, 0, 0), research_runner=runner))
    assert db.get_app_config("re_research_last") is None


def test_is_remote_node_checked_unconditionally_outside_window(fresh_db):
    check_calls = []

    def spy_is_remote_node():
        check_calls.append(True)
        return False

    with mock.patch.object(db, "is_remote_node", side_effect=spy_is_remote_node):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 12, 30, 0)))
    assert len(check_calls) == 1
