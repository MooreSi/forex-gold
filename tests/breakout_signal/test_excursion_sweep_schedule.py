"""The breakout excursion backfill on the nightly timer.

It is measurement, and the nightly settlement-break slot already exists for
exactly that: `study_schedule` runs the reversal engine's research study
there, and that study already backfills the reversal engine's excursions the
same way. This engine's numbers should be gathered in the same window and by
the same clock, not left to a button somebody remembers to press -- which is
how the reversal engine's study went four days stale (docs/todo/bugs/030's
neighbour, `reversal-engine/210`).

It keeps its OWN app_config date key and its own try/except, so it and the
two jobs beside it cannot take each other's day down.

Nothing here places, closes or modifies a trade.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest import mock

from backend.src.db import database as db
from backend.src.services.breakout_signal import excursion_sweep


def _at(hour, day=20):
    return datetime(2026, 7, day, hour, 0, 0)


def _sweep(now, *, remote=False, runner=None):
    calls = []

    async def _default(limit=500):
        calls.append(limit)
        return {"summary": "3 measured"}

    async def _go():
        with mock.patch.object(db, "is_remote_node", return_value=remote), \
             mock.patch("backend.src.services.breakout_signal."
                        "excursion_backfill.run", side_effect=runner or _default):
            await excursion_sweep.breakout_excursion_sweep(None, now=now)

    asyncio.run(_go())
    return calls


class TestWhenItRuns:
    def test_before_the_slot_it_does_not_run(self, fresh_db):
        assert _sweep(_at(21)) == []
        assert db.get_app_config(excursion_sweep.LAST_RUN_KEY) is None

    def test_at_the_slot_it_runs_and_claims_the_day(self, fresh_db):
        assert len(_sweep(_at(22))) == 1
        assert db.get_app_config(excursion_sweep.LAST_RUN_KEY) == "2026-07-20"

    def test_it_does_not_run_twice_in_a_day(self, fresh_db):
        db.set_app_config(excursion_sweep.LAST_RUN_KEY, "2026-07-20")
        assert _sweep(_at(22)) == []

    def test_a_remote_node_never_runs_it(self, fresh_db):
        """It writes measurement columns to the local database through the
        local engine's bridge, same gate as the two jobs beside it."""
        assert _sweep(_at(22), remote=True) == []


class TestWhatItRefusesToDo:
    def test_a_backfill_that_errors_does_not_claim_the_day(self, fresh_db):
        async def _err(limit=500):
            return {"error": "the breakout engine is not running"}

        _sweep(_at(22), runner=_err)
        assert db.get_app_config(excursion_sweep.LAST_RUN_KEY) is None

    def test_a_backfill_that_raises_does_not_claim_the_day(self, fresh_db):
        async def _boom(limit=500):
            raise RuntimeError("bridge down")

        _sweep(_at(22), runner=_boom)
        assert db.get_app_config(excursion_sweep.LAST_RUN_KEY) is None

    def test_a_backfill_that_raises_does_not_raise_into_the_loop(self, fresh_db):
        async def _boom(limit=500):
            raise RuntimeError("bridge down")

        _sweep(_at(22), runner=_boom)   # no exception escapes


class TestItIsWiredIntoTheTimer:
    def test_the_minute_loop_calls_it(self):
        called = []

        async def _noop(engine):
            return None

        async def _mine(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        async def _go():
            base = "backend.src.services.reversal_engine.research_loop."
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch(base + "_reversal_engine_research_sweep_impl",
                            side_effect=_noop), \
                 mock.patch(base + "_reversal_engine_study_sweep_impl",
                            side_effect=_noop), \
                 mock.patch(base + "_breakout_excursion_sweep_impl",
                            side_effect=_mine):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]
