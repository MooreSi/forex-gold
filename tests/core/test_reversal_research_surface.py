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
from datetime import datetime

import pytest
from unittest import mock

from backend.src.db import database as db
from backend.src.services.reversal_engine import research as re_research


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


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


# ── window widening (2026-08-18) ─────────────────────────────────────────────
#
# The sweep used to require `now.hour == 22 and now.minute == 0`, a single
# minute. Its caller sleeps 60s per cycle PLUS however long the sweep took, so
# the checks drift and minute 0 is easy to step straight over; and an app
# restarted at 22:00:30 missed the day outright, because the loop's own 90s
# settle delay put its first check past the window. 2026-08-09, 08-14 and
# 08-15 have no research row for exactly this reason, leaving
# ref_discipline_score / ref_aggression_score -- two live ML features -- on
# whatever the last successful night wrote.

def _runner_recording(calls):
    async def runner(engine):
        calls.append(engine)
        return {"ran": True}
    return runner


@pytest.mark.parametrize("hh,mm", [(22, 0), (22, 1), (22, 30), (23, 0), (23, 59)])
def test_runs_any_time_from_2200_onward(fresh_db, hh, mm):
    """Missing minute 0 must cost a few minutes, not the whole day."""
    calls = []
    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, hh, mm, 0),
            research_runner=_runner_recording(calls)))
    assert calls == ["engine"], f"skipped at {hh:02d}:{mm:02d}"
    assert db.get_app_config("re_research_last") == "2026-07-20"


@pytest.mark.parametrize("hh,mm", [(0, 0), (12, 0), (21, 0), (21, 59)])
def test_still_does_not_run_before_2200(fresh_db, hh, mm):
    """Widening the window must not turn it into an any-time job -- it reads
    the day's messages, so it has to run after the day is over."""
    calls = []
    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, hh, mm, 0),
            research_runner=_runner_recording(calls)))
    assert calls == []
    assert db.get_app_config("re_research_last") is None


def test_the_wider_window_cannot_double_run_within_a_day(fresh_db):
    """A whole evening of 60s cycles now falls inside the window, so the date
    key is doing real work rather than only covering a restart near 22:00."""
    calls = []
    runner = _runner_recording(calls)
    with mock.patch.object(db, "is_remote_node", return_value=False):
        for mm in (0, 1, 5, 30, 59):
            asyncio.run(re_research.reversal_engine_research_sweep(
                "engine", now=datetime(2026, 7, 20, 22, mm, 0), research_runner=runner))
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 23, 30, 0), research_runner=runner))
    assert len(calls) == 1


def test_a_restart_at_220030_still_gets_that_days_research(fresh_db):
    """The exact miss: the app comes up half a minute past the old window, and
    its first check lands 90s later."""
    calls = []
    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 8, 14, 22, 2, 0),
            research_runner=_runner_recording(calls)))
    assert calls == ["engine"]
    assert db.get_app_config("re_research_last") == "2026-08-14"


def test_a_failed_run_is_retried_on_the_next_cycle(fresh_db):
    """result.ran False must not mark the day done -- otherwise one transient
    failure silently costs the day's features."""
    attempts = []

    async def flaky(engine):
        attempts.append(engine)
        return {"ran": False} if len(attempts) == 1 else {"ran": True}

    with mock.patch.object(db, "is_remote_node", return_value=False):
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 22, 0, 0), research_runner=flaky))
        assert db.get_app_config("re_research_last") is None
        asyncio.run(re_research.reversal_engine_research_sweep(
            "engine", now=datetime(2026, 7, 20, 22, 1, 0), research_runner=flaky))
    assert len(attempts) == 2
    assert db.get_app_config("re_research_last") == "2026-07-20"
