"""The research study's arithmetic must not run on the trading loop.

`_render_research_section`'s **Run study** button blocked the event loop for
**21.492 seconds** on 2026-09-12 (`docs/todo/bugs/030`). That is the loop order
dispatch, position monitoring, EA messages and Telegram alerts all share: for
21 seconds the app could not have placed, managed or closed anything.

`run_study` awaits its bridge reads properly. What it then does inline is
arithmetic, and the expensive piece is `barrier_fit.sweep`: six stops by five
targets, each replaying every one of 250 tick paths. Benchmarked on this
machine against paths of the length `_paths_for` actually builds:

    400 points/path    3.45 s
  2,000 points/path    9.97 s
  6,000 points/path   24.93 s

`barrier_fit` imports `random`, `dataclasses` and `typing` — no database, no
bridge, no I/O of any kind. Pure functions can be handed to a thread with
identical results, which is what these tests pin: not "it is faster", but
"it is not on the loop".
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from backend.src.services.market import barrier_fit
from backend.src.services.reversal_engine import research_lab

pytestmark = pytest.mark.asyncio


class _Bridge:
    """Enough surface for run_study's reads, and nothing that can trade."""

    async def get_tick(self):
        return None

    async def get_candles(self, *a, **k):
        return []

    async def get_ticks_range(self, *a, **k):
        return []

    async def get_deal_history(self, *a, **k):
        return []

    async def get_position_history(self, *a, **k):
        return []

    async def get_account(self):
        return {"balance": 1000.0, "equity": 1000.0}


@pytest.fixture
def watch_threads(monkeypatch):
    """Record which thread each heavy call ran on."""
    seen: dict[str, threading.Thread] = {}

    def _spy(name, real):
        def _wrapped(*a, **k):
            seen[name] = threading.current_thread()
            return real(*a, **k)
        return _wrapped

    for name in ("sweep", "fit_barriers", "reach_distribution"):
        monkeypatch.setattr(barrier_fit, name,
                            _spy(name, getattr(barrier_fit, name)))
    return seen


async def _run(monkeypatch, paths):
    """run_study with every read stubbed, so only the arithmetic is exercised."""
    monkeypatch.setattr(research_lab, "probe", lambda *a, **k: _async({}))
    monkeypatch.setattr(research_lab, "measure_costs", lambda *a, **k: _async({}))
    monkeypatch.setattr(research_lab, "_paths_for", lambda *a, **k: _async(paths))
    monkeypatch.setattr(research_lab.excursion_backfill, "backfill",
                        lambda *a, **k: _async(type("R", (), {"__dict__": {}})()))
    monkeypatch.setattr(research_lab.correlation, "snapshot", lambda *a, **k: _async({}))
    monkeypatch.setattr(research_lab.measure_repo, "closed_executed_rows", lambda: [])
    monkeypatch.setattr(research_lab.measure_repo, "training_vectors", lambda: [])
    monkeypatch.setattr(research_lab.measure_repo, "excursion_observations", lambda: [])
    monkeypatch.setattr(research_lab.attribution, "cohorts", lambda rows: {})
    monkeypatch.setattr(research_lab.attribution, "render", lambda c: "")
    monkeypatch.setattr(research_lab.macro_backfill, "backfill", lambda *a, **k: {})
    monkeypatch.setattr(research_lab, "_save_summary", lambda r: None)
    return await research_lab.run_study(_Bridge())


async def _async(value):
    return value


def _path(i):
    ts = 1789000000.0
    return {"path": [(ts + n, 4300.5, 4299.5) for n in range(40)],
            "entry": 4300.0, "direction": "BUY", "close_time": ts + i}


class TestTheArithmeticRunsOffTheLoop:
    async def test_the_sweep_does_not_run_on_the_event_loop_thread(
            self, monkeypatch, watch_threads):
        await _run(monkeypatch, [_path(i) for i in range(4)])

        assert "sweep" in watch_threads, "the sweep did not run at all"
        assert watch_threads["sweep"] is not threading.current_thread()

    async def test_the_barrier_fit_does_not_either(self, monkeypatch, watch_threads):
        await _run(monkeypatch, [_path(i) for i in range(4)])

        assert watch_threads["fit_barriers"] is not threading.current_thread()

    async def test_nor_the_reach_distribution(self, monkeypatch, watch_threads):
        await _run(monkeypatch, [_path(i) for i in range(4)])

        assert watch_threads["reach_distribution"] is not threading.current_thread()

    async def test_the_loop_keeps_ticking_while_the_study_runs(
            self, monkeypatch, watch_threads):
        """The property the 21-second block violated, stated directly: another
        task on the same loop must still get to run."""
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        t = asyncio.get_running_loop().create_task(_ticker())
        await _run(monkeypatch, [_path(i) for i in range(4)])
        t.cancel()

        assert ticks > 0


class TestItStillProducesTheSameReport:
    async def test_the_report_carries_the_sweep_and_the_fit(self, monkeypatch):
        report = await _run(monkeypatch, [_path(i) for i in range(4)])

        assert "barrier_fit" in report
        assert "reach" in report
        assert "sweep" in report

    async def test_no_paths_means_no_sweep_section(self, monkeypatch):
        """Unchanged behaviour: the sweep is skipped when there is nothing to
        replay, rather than reported as an empty result."""
        report = await _run(monkeypatch, [])

        assert "sweep" not in report
