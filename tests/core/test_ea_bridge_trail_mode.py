"""EABridge.open_trade()'s trail_mode wire field -- new for Adaptive Runner
2, the first ladder-shaped strategy whose SL rule (trail to the midpoint of
the two TPs before the one just hit) isn't representable through the
pcts/be_at_pos protocol alone; see ForexTraderBridge.mq5's ManageLadder()
for the EA-side branch this must stay in lockstep with."""
import asyncio
import json
import time

import pytest

from forex_trader.core import ea_bridge


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


def _sent_message(bridge) -> dict:
    raw = bridge._writer.written[-1].decode().strip()
    return json.loads(raw)


def test_adaptive_runner_2_is_ea_portable():
    assert "adaptive_runner_2" in ea_bridge.EA_PORTABLE_STRATEGIES


def test_open_trade_sends_trail_mode_when_provided():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-1", "BUY", 0.10, 2390.0, {1: 2410.0}, "adaptive_runner_2",
            pcts=[1.00], be_at_pos=1, trail_mode="midpoint_lag2", timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["trail_mode"] == "midpoint_lag2"
    assert msg["be_at_pos"] == 1
    assert msg["strategy"] == "adaptive_runner_2"


def test_open_trade_omits_trail_mode_when_not_provided():
    """Every strategy before Adaptive Runner 2 never passes trail_mode --
    the key must not appear on the wire at all, matching the EA's own
    default-to-prev-TP fallback when the field is absent."""
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-2", "BUY", 0.10, 2390.0, {1: 2410.0}, "gd_vip_runner",
            pcts=[1.00], be_at_pos=1, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert "trail_mode" not in msg
