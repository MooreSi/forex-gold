"""The EA link's round trip, for Settings > Latency (docs/todo/006).

The EA already answers a "ping" with a "pong" (ForexTraderBridge.mq5's message
handler); Python simply never sent one. The round trip is TCP plus however
long the EA takes to read its socket -- a 200 ms timer, or longer while
OnTick is busy managing trades. Nothing here reaches an order.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.src.services.broker import ea_bridge


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def _healthy_bridge():
    bridge = ea_bridge.EABridge(engine=None)
    bridge._writer = _FakeWriter()
    bridge._last_seen = time.time()
    return bridge


@pytest.mark.asyncio
async def test_a_pong_completes_the_round_trip():
    bridge = _healthy_bridge()

    async def _ea_answers():
        await asyncio.sleep(0.01)
        await bridge._dispatch({"type": "pong"})

    asyncio.get_running_loop().create_task(_ea_answers())
    out = await bridge.ping_ms(timeout=1.0)

    assert out["ok"] is True
    assert 5.0 <= out["ms"] < 1000.0
    assert json.loads(bridge._writer.written[-1].decode()) == {"type": "ping"}


@pytest.mark.asyncio
async def test_silence_is_reported_not_raised():
    bridge = _healthy_bridge()

    out = await bridge.ping_ms(timeout=0.05)

    assert out["ok"] is False
    assert "no answer" in out["detail"]


@pytest.mark.asyncio
async def test_no_ea_is_reported_without_sending():
    bridge = ea_bridge.EABridge(engine=None)

    out = await bridge.ping_ms(timeout=0.05)

    assert out == {"ok": False, "ms": None, "detail": "EA not connected"}


@pytest.mark.asyncio
async def test_an_unrequested_pong_is_harmless():
    bridge = _healthy_bridge()

    await bridge._dispatch({"type": "pong"})

    out = await bridge.ping_ms(timeout=0.05)
    assert out["ok"] is False
