"""The research study on a schedule, and what it refuses to do unattended.

The study is the only source of the reach distribution and the exit-policy
sweep, and `ai_tuner.gather_evidence` can only READ it -- it cannot refresh
it. Left to a button, it went four days stale (last run 2026-09-12, asked
for on 2026-09-16) while the tuner kept presenting those numbers as current.

Nothing here places, closes or modifies a trade. The study reads history and
writes two measurement columns.
"""
import asyncio
from datetime import datetime
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.reversal_engine import study_schedule


_RUN_STUDY = "backend.src.services.reversal_engine.research_lab.run_study"


def _at(hour, minute=0, day=20):
    return datetime(2026, 7, day, hour, minute, 0)


class _Bridge:
    """A bridge-shaped sentinel. It is never asked for anything: run_study is
    patched out in every test here, because a real study is hundreds of
    broker round trips."""


def _sweep(now, *, bridge=_Bridge(), remote=False, runner=None, engine=None):
    calls = []

    async def _default(bridge_arg, **kwargs):
        calls.append(bridge_arg)
        return {"ran_at": 1.0}

    async def _go():
        with mock.patch.object(db, "is_remote_node", return_value=remote), \
             mock.patch(_RUN_STUDY, side_effect=runner or _default), \
             mock.patch.object(study_schedule, "_bridge_of_running_engine",
                               return_value=bridge):
            await study_schedule.reversal_engine_study_sweep(engine, now=now)

    asyncio.run(_go())
    return calls


class TestWhenItRuns:
    def test_before_the_slot_it_does_not_run_or_claim_the_day(self, fresh_db):
        assert _sweep(_at(21, 59)) == []
        assert db.get_app_config("re_study_last") is None

    def test_at_the_slot_it_runs_once_and_claims_the_day(self, fresh_db):
        assert len(_sweep(_at(22, 0))) == 1
        assert db.get_app_config("re_study_last") == "2026-07-20"

    def test_past_the_slot_it_still_runs(self, fresh_db):
        """The caller sleeps 60s per cycle PLUS however long the sweep took,
        so the checks drift and the single minute 22:00 is easy to step over.
        Same reason the nightly Telegram sweep uses `>= 22` rather than `== 22`
        -- three days in August have no research row from exactly that bug."""
        assert len(_sweep(_at(23, 30))) == 1

    def test_having_run_today_it_does_not_run_again(self, fresh_db):
        db.set_app_config("re_study_last", "2026-07-20")
        assert _sweep(_at(22, 0)) == []

    def test_a_new_day_runs_again(self, fresh_db):
        db.set_app_config("re_study_last", "2026-07-20")
        assert len(_sweep(_at(22, 0, day=21))) == 1
        assert db.get_app_config("re_study_last") == "2026-07-21"

    def test_a_remote_node_never_runs_it(self, fresh_db):
        """Same gate the nightly sweep uses. The study reads through the
        bridge the LOCAL engine trades on and writes measurement columns to
        the local database; a second node doing it duplicates the broker load
        and measures a book it is not keeping."""
        assert _sweep(_at(22, 0), remote=True) == []
        assert db.get_app_config("re_study_last") is None


class TestWhatItRefusesToDo:
    def test_with_no_engine_running_it_does_not_claim_the_day(self, fresh_db):
        """No bridge means no study. Marking the day done here would burn the
        only run of it until tomorrow, which is precisely the staleness this
        schedule exists to stop."""
        assert _sweep(_at(22, 0), bridge=None) == []
        assert db.get_app_config("re_study_last") is None

    def test_a_study_that_raises_does_not_claim_the_day(self, fresh_db):
        async def _boom(bridge, **kwargs):
            raise RuntimeError("the broker dropped the tick history")

        _sweep(_at(22, 0), runner=_boom)
        assert db.get_app_config("re_study_last") is None

    def test_a_study_that_raises_does_not_raise_into_the_loop(self, fresh_db):
        async def _boom(bridge, **kwargs):
            raise RuntimeError("the broker dropped the tick history")

        _sweep(_at(22, 0), runner=_boom)   # no exception escapes

    def test_it_runs_the_study_against_the_running_engines_bridge(self, fresh_db):
        """Not a connection of its own. The study has to read the history of
        the same broker the engine trades on."""
        bridge = _Bridge()
        assert _sweep(_at(22, 0), bridge=bridge) == [bridge]


class TestItIsWiredIntoTheLoopThatAlreadyTicks:
    """A new asyncio task in runtime.py would be a second thing to start,
    stop and forget. The nightly research loop already ticks every minute at
    exactly the right hour."""

    def test_the_minute_loop_calls_the_study_sweep(self):
        called = []

        async def _telegram(engine):
            return None

        async def _study(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        async def _go():
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch("backend.src.services.reversal_engine.research_loop"
                            "._reversal_engine_research_sweep_impl",
                            side_effect=_telegram), \
                 mock.patch("backend.src.services.reversal_engine.research_loop"
                            "._reversal_engine_study_sweep_impl",
                            side_effect=_study):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]

    def test_a_failing_telegram_sweep_does_not_stop_the_study(self):
        """They are independent jobs sharing a timer. One dying must not take
        the other's day with it."""
        called = []

        async def _telegram(engine):
            raise RuntimeError("Telegram is down")

        async def _study(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        async def _go():
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch("backend.src.services.reversal_engine.research_loop"
                            "._reversal_engine_research_sweep_impl",
                            side_effect=_telegram), \
                 mock.patch("backend.src.services.reversal_engine.research_loop"
                            "._reversal_engine_study_sweep_impl",
                            side_effect=_study):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]
