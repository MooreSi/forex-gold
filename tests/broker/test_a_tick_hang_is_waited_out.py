"""A freeze the client knows is a hung tick read is waited out, not restarted.

2026-10-19, owner's Mac, after the tick-read pause shipped. Each time the
pause ran out, the next tick read hung. That froze the bridge for 60-70s, and
then it came back by itself. The self-healer still restarted it, 31s into the
freeze at 21:22:13, and so cost an MT5 cold start for a stall that was
clearing anyway. The client now remembers when a tick read hung, and both
restarters hold off for TICK_HANG_HOLD_S after it. A bridge that is still
dead after that is restarted as before.

NOTHING HERE REACHES A BRIDGE OR A BROKER. Bridges are fakes, restarts are
counted, alerts are captured.
"""
from __future__ import annotations

import asyncio
import time
from unittest import mock

import httpx
import pytest

from backend.src.services.broker import mt5_client as mc
from backend.src.services.broker import watchdog
from backend.src.services.health import self_healer as sh
from backend.src.services.telegram import alerts as telegram_alerts


class _HungRecently:
    """Stands in for the client: answers whether a tick read hung lately."""
    def __init__(self, hung: bool, health=None):
        self.hung = hung
        self._health = health or {"connected": False}

    def tick_read_hung_recently(self) -> bool:
        return self.hung

    async def get_health(self):
        return self._health


class TestTheClientRemembersAHang:
    def test_no_hang_means_no_hold(self, monkeypatch):
        monkeypatch.setattr(mc, "_ticks_clock", lambda: 1000.0)
        assert mc.MT5BridgeClient("http://x").tick_read_hung_recently() is False

    def test_a_hang_holds_for_the_hold_period_then_lets_go(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(mc, "_ticks_clock", lambda: now["t"])
        client = mc.MT5BridgeClient("http://x")

        async def _times_out(method, url, timeout=10.0, **kw):
            raise httpx.ReadTimeout("timed out")
        client._request = _times_out
        asyncio.run(client.get_ticks_range(1.0, 2.0))

        now["t"] += mc.TICK_HANG_HOLD_S - 1
        assert client.tick_read_hung_recently() is True
        now["t"] += 2
        assert client.tick_read_hung_recently() is False

    def test_the_hold_covers_a_whole_observed_freeze(self):
        """Observed freezes after a hung read: 60-70s."""
        assert mc.TICK_HANG_HOLD_S >= 90


def _run_watchdog(bridge, cycles):
    state = {"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}
    starts = []

    async def fake_start():
        starts.append(1)
        return True

    async def fake_send(msg):
        pass

    async def go():
        clock = iter(10_000.0 + i for i in range(cycles + 5))
        with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send), \
             mock.patch.object(watchdog, "monotonic", lambda: next(clock)):
            for _ in range(cycles):
                await watchdog.bridge_watchdog_check(bridge, state, False, fake_start)
    asyncio.run(go())
    return starts


class TestTheWatchdogWaitsOutATickHang:
    def test_two_failed_checks_during_a_tick_hang_do_not_restart(self, fresh_db):
        assert _run_watchdog(_HungRecently(hung=True), 3) == []

    def test_two_failed_checks_without_a_tick_hang_still_restart(self, fresh_db):
        assert _run_watchdog(_HungRecently(hung=False), 2) == [1]


class _Engine:
    def __init__(self, bridge):
        self._started_at = None
        self._bridge = bridge
        self.bridge_starts = 0

    async def start_bridge_process(self):
        self.bridge_starts += 1
        return True


@pytest.fixture
def quiet(monkeypatch):
    async def _fake(condition, action):
        pass
    monkeypatch.setattr(sh, "_send_heal_notification", _fake)


def _lines(text, n):
    now = time.time()
    return [(now, f"2026-10-19 21:22:00 WARNING mod — {text}") for _ in range(n)]


@pytest.mark.asyncio
class TestTheHealerWaitsOutATickHang:
    async def test_it_does_not_restart_during_a_tick_hang(self, monkeypatch, quiet):
        monkeypatch.setattr(sh, "_read_recent_log_lines", lambda w: _lines(
            "bridge not responding to tick requests (17 consecutive failures)", 17))
        engine = _Engine(_HungRecently(hung=True))

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 0

    async def test_it_still_restarts_without_one(self, monkeypatch, quiet):
        monkeypatch.setattr(sh, "_read_recent_log_lines", lambda w: _lines(
            "bridge not responding to tick requests (17 consecutive failures)", 17))
        engine = _Engine(_HungRecently(hung=False))

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 1
