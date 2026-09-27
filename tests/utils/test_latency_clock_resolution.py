"""Latency timings must not use a clock that ticks every 15.6 ms.

On Python 3.11 for Windows -- the VPS and the CI runner -- `time.monotonic()`
is GetTickCount64, which advances in ~15.6 ms steps. A 2 ms gap measured 0.0
and a 10 ms EA round trip measured 0.0 (CI, 2026-09-26 and 09-27:
test_real_clock_smoke and test_a_pong_completes_the_round_trip). On the VPS,
Settings > Latency would show every hop under ~16 ms as 0.

Reproduced here on any platform by freezing `time.monotonic`: a coarse clock
that has not ticked yet. `time.perf_counter()` is high-resolution everywhere.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.services.broker import ea_bridge
from backend.src.utils import latency_trace as lt


@pytest.fixture
def coarse_monotonic(monkeypatch):
    frozen = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: frozen)


@pytest.fixture(autouse=True)
def _clean():
    lt.clear()
    yield
    lt.clear()


def test_a_trace_gap_is_measured_below_the_tick(coarse_monotonic):
    lt.mark("r", "a")
    time.sleep(0.002)
    lt.mark("r", "b")

    assert lt.gap_ms("r", "a", "b") >= 1.0


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def test_an_ea_round_trip_is_measured_below_the_tick(coarse_monotonic):
    bridge = ea_bridge.EABridge(engine=None)
    bridge._writer = _FakeWriter()
    bridge._last_seen = time.time()

    async def _run():
        async def _ea_answers():
            time.sleep(0.01)    # not asyncio.sleep: its timer runs on the frozen clock
            await bridge._dispatch({"type": "pong"})
        asyncio.get_running_loop().create_task(_ea_answers())
        return await bridge.ping_ms(timeout=1.0)

    out = asyncio.run(_run())

    assert out["ok"] is True
    assert out["ms"] >= 5.0
