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
import json
import time

import pytest

from backend.src.services.broker import ea_bridge
from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.diagnostics import latency as diag
from backend.src.services.telegram.reader import AUTH_CONNECTED, TelegramReader
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


# ── The three 7d9460e2 missed (CI, 2026-09-27, run 36343576570) ─────────────
# test_the_round_trip_and_the_report_come_back read a 10 ms VPS round trip as
# 0.0 and test_it_times_one_light_request a 10 ms Telegram request as 0.0.

class _VpsWs:
    """Plays the VPS: answers a probe ping after 10 ms of real time."""

    def __init__(self, client):
        self.client = client

    async def send(self, raw):
        msg = json.loads(raw)
        if msg.get("type") != P.MSG_PING:
            return

        async def _answer():
            time.sleep(0.01)    # not asyncio.sleep: its timer runs on the frozen clock
            await self.client._dispatch({"type": P.MSG_PONG, "probe_id": msg["probe_id"]})
            await self.client._dispatch({"type": P.MSG_PONG, "probe_id": msg["probe_id"],
                                         "latency": {"probes": {}}})
        asyncio.get_running_loop().create_task(_answer())


def test_a_vps_round_trip_is_measured_below_the_tick(coarse_monotonic):
    c = sync_client.SyncClient()
    c.conn_state = sync_client.CONN_CONNECTED
    c._ws = _VpsWs(c)

    out = asyncio.run(c.probe_peer(timeout=1.0))

    assert out["ok"] is True
    assert out["rtt_ms"] >= 5.0


class _TelegramClient:
    session = type("S", (), {"dc_id": 4})()

    async def __call__(self, request):
        time.sleep(0.01)        # not asyncio.sleep: its timer runs on the frozen clock
        return type("N", (), {"this_dc": 4, "nearest_dc": 2})()


def test_a_telegram_round_trip_is_measured_below_the_tick(coarse_monotonic, tmp_path):
    r = TelegramReader({"sessions_dir": str(tmp_path)})
    r._client = _TelegramClient()
    r._auth_state = AUTH_CONNECTED

    out = asyncio.run(r.api_round_trip())

    assert out["ok"] is True
    assert out["ms"] >= 5.0


def test_a_diagnostics_probe_is_measured_below_the_tick(coarse_monotonic):
    async def _ten_ms():
        time.sleep(0.01)

    out = asyncio.run(diag._timed("loop", _ten_ms, lambda _v: (True, "")))

    assert out["ms"] >= 5.0
