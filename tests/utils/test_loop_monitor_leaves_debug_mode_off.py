"""Starting the stall watchdog must not switch asyncio into debug mode.

LoopMonitor used to call `loop.set_debug(True)` so asyncio would name the
slow task. Debug mode records a source traceback for every Handle and Task it
creates, and building one runs `linecache.checkcache` -- an `os.stat` per
frame's file. Each stat releases the GIL, and while a worker thread is doing
CPU work the loop then waits for it to hand the GIL back, once per stat.

Measured on this Mac (3.13) with one CPU-bound `asyncio.to_thread` running:
162 loop iterations in 3 s with debug off, 3 with it on. With no thread at all
debug mode was still 15x slower. That is the nightly 22:00 stall: the
Reversal Engine study's sweep runs on a thread, and every stall the sampler
caught (2026-09-26, 22:00:35 to 22:00:44, up to 4.4 s) was inside
`linecache.py checkcache`, reached from whichever task happened to create a
Handle. The diagnostic was the cause.

The sampler thread names the blocking line without debug mode, so nothing is
lost that the sampler does not already give.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.utils import loop_monitor


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(loop_monitor, "_stalls", [])
    monkeypatch.setattr(loop_monitor, "_task", None)
    yield
    loop_monitor.stop_sampler()


def test_start_leaves_the_loop_out_of_debug_mode():
    async def _run() -> bool:
        loop_monitor.start()
        await asyncio.sleep(0)
        return asyncio.get_running_loop().get_debug()

    assert asyncio.run(_run(), debug=False) is False
