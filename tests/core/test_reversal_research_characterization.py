"""Characterizes SimulationEngine._reversal_engine_research_loop's per-cycle
check-and-run body (core/engine.py) against UNMODIFIED engine.py, ahead of
extraction into forex_trader/core/core_reversal_research.py -- see
docs/todo/refactor/core-reversal-research-migration/010-*.md.

Gates a nightly ML-feature research job -- no MT5 order is ever placed,
closed, or modified.
"""
import asyncio
import os
import tempfile
from datetime import datetime
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core.engine import SimulationEngine


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


def _patched_now(fixed_dt):
    """Context manager patching core_reversal_research.datetime.now() (where
    engine.py's now-wired _reversal_engine_research_loop actually computes the
    current time, having delegated to reversal_engine_research_sweep) while leaving
    direct datetime(...) construction working via the real class."""
    patcher = mock.patch("forex_trader.core.core_reversal_research.datetime")
    mock_dt = patcher.start()
    mock_dt.now.return_value = fixed_dt
    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    return patcher


def _make_engine():
    e = SimulationEngine.__new__(SimulationEngine)
    e._monitor_running = True
    return e


def _stop_after_second_sleep(engine):
    calls = {"n": 0}

    async def _sleep(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            engine._monitor_running = False

    return _sleep


_TARGET = "backend.src.services.reversal_engine.telegram_research.run_nightly_research"


def test_not_2200_no_pipeline_call_no_dedup_write(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 21, 59, 0))
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert calls == []
    assert db.get_app_config("re_research_last") is None


def test_remote_node_skips_even_at_2200(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 22, 0, 0))
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", return_value=True), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert calls == []


def test_already_ran_today_skips(fresh_db):
    db.set_app_config("re_research_last", "2026-07-20")
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 22, 0, 0))
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", return_value=False), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert calls == []


def test_runs_with_engine_and_marks_dedup_when_ran_true(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 22, 0, 0))
    calls = []

    async def runner(engine):
        calls.append(engine)
        return {"ran": True}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", return_value=False), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert calls == [e]
    assert db.get_app_config("re_research_last") == "2026-07-20"


def test_ran_false_does_not_mark_dedup(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 22, 0, 0))

    async def runner(engine):
        return {"ran": False}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", return_value=False), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert db.get_app_config("re_research_last") is None


def test_pipeline_exception_swallowed_no_dedup_write(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 22, 0, 0))

    async def runner(engine):
        raise RuntimeError("pipeline boom")

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", return_value=False), \
             mock.patch(_TARGET, side_effect=runner):
            asyncio.run(e._reversal_engine_research_loop())  # must not raise
    finally:
        p.stop()
    assert db.get_app_config("re_research_last") is None


def test_is_remote_node_checked_unconditionally_outside_window(fresh_db):
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 12, 30, 0))
    check_calls = []

    def spy_is_remote_node():
        check_calls.append(True)
        return False

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(db, "is_remote_node", side_effect=spy_is_remote_node):
            asyncio.run(e._reversal_engine_research_loop())
    finally:
        p.stop()
    assert len(check_calls) == 1
