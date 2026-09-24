"""A stall warning says which line was blocking, not just which tasks exist.

The daily 22:00 stall (1-7 s, every night since at least 2026-09-14) could not
be attributed. The monitor wakes on the loop, so it only learns of a stall
after it has ended, and all it could say was the sorted list of every task in
the process -- "BreakoutEngine._cycle_loop, ..." every time, alphabetically,
whatever the cause. asyncio's debug warning names the task that was running
but not the line: `_signal_scanner_loop took 4.878 seconds`, and that loop
runs a hundred database calls a cycle.

A sampler thread now watches the loop's heartbeat from outside it. When the
loop has been silent past the threshold it reads the loop thread's stack --
where the loop IS, while it is stuck -- and the warning carries it.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from backend.src.utils import loop_monitor


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(loop_monitor, "_stalls", [])
    monkeypatch.setattr(loop_monitor, "_task", None)
    yield
    loop_monitor.stop_sampler()


def _deliberately_blocking_step(seconds: float) -> None:
    time.sleep(seconds)


async def _scenario(block_s: float) -> None:
    loop_monitor.start()
    await asyncio.sleep(0.6)            # let the watchdog and sampler settle
    _deliberately_blocking_step(block_s)
    await asyncio.sleep(0.6)            # let the watchdog wake and report


def test_the_warning_names_the_function_that_blocked(caplog):
    caplog.set_level(logging.WARNING, logger=loop_monitor.log.name)

    asyncio.run(_scenario(1.0))

    stalls = [r.getMessage() for r in caplog.records if "stalled" in r.getMessage()]
    assert stalls, "the stall was not reported at all"
    assert any("_deliberately_blocking_step" in m for m in stalls), stalls


def test_the_stack_is_kept_for_the_dashboard_too():
    asyncio.run(_scenario(1.0))

    recorded = loop_monitor.recent_stalls()
    assert recorded
    assert any("_deliberately_blocking_step" in s.get("stack", "") for s in recorded)


def test_it_names_this_repo_s_frames_not_the_standard_library(caplog):
    """`time.sleep` is where the thread physically is. The line worth reading
    is the project code that called it."""
    caplog.set_level(logging.WARNING, logger=loop_monitor.log.name)

    asyncio.run(_scenario(1.0))

    msg = next(r.getMessage() for r in caplog.records if "stalled" in r.getMessage())
    assert "test_loop_monitor_names_the_blocking_line.py" in msg


def test_a_healthy_loop_reports_nothing(caplog):
    caplog.set_level(logging.WARNING, logger=loop_monitor.log.name)

    async def _quiet():
        loop_monitor.start()
        await asyncio.sleep(1.2)

    asyncio.run(_quiet())

    assert not [r for r in caplog.records if "stalled" in r.getMessage()]
