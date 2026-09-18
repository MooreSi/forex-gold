"""Characterizes SimulationEngine._reversal_engine_research_loop's per-cycle
check-and-run body (core/engine.py) against UNMODIFIED engine.py, ahead of
extraction into forex_trader/core/core_reversal_research.py -- see
docs/todo/refactor/core-reversal-research-migration/010-*.md.

Gates a nightly ML-feature research job -- no MT5 order is ever placed,
closed, or modified.
"""
import asyncio
from datetime import datetime
from unittest import mock


from backend.src.db import database as db
from backend.src.runtime import TradingRuntime


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


# Every module whose clock the research loop reads. It calls THREE sweeps, and
# each one computes its own `datetime.now(...)` in its own namespace.
#
# Only the first was pinned until 2026-09-18, so the other two were reading the
# real wall clock: both return early while the London hour is under 22 and run
# past their `is_remote_node()` check once it is 22 or later. That made
# `test_is_remote_node_checked_unconditionally_outside_window` pass all day and
# fail every night between 22:00 and midnight London time, on any machine --
# which is exactly what it did on CI at 22:22 BST. A test that means something
# different depending on when it runs is not pinning anything.
_CLOCK_MODULES = (
    "backend.src.services.reversal_engine.research",
    "backend.src.services.reversal_engine.study_schedule",
    "backend.src.services.breakout_signal.excursion_sweep",
)


class _Patchers:
    """The handful of clock patches, stopped together."""

    def __init__(self, patchers):
        self._patchers = patchers

    def stop(self):
        for patcher in self._patchers:
            patcher.stop()


def _patched_now(fixed_dt):
    """Pin the clock every sweep in the loop reads, at `fixed_dt`.

    Direct `datetime(...)` construction keeps working via the real class, which
    is why each mock gets a side_effect rather than being a bare MagicMock.
    """
    started = []
    for module in _CLOCK_MODULES:
        patcher = mock.patch(f"{module}.datetime")
        mock_dt = patcher.start()
        mock_dt.now.return_value = fixed_dt
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        started.append(patcher)
    return _Patchers(started)


def _make_engine():
    e = TradingRuntime.__new__(TradingRuntime)
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
    """The research sweep checks the node role before anything else, even at
    12:30 when it has no work to do.

    The count is 1 because the loop's other two sweeps return on their own
    hour check first. That is only true if their clocks are pinned too — see
    `_CLOCK_MODULES`.
    """
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
