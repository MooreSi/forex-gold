"""Proves backend.src.services.broker.watchdog.bridge_watchdog_check
behaves identically to SimulationEngine's original, characterized in
test_bridge_watchdog_characterization.py -- see
docs/todo/refactor/core-bridge-watchdog-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class (state dict, bridge_inhibit_reconnect, start_bridge_process passed
explicitly). No real or demo MT5 order is ever placed, closed, or
modified.
"""
import asyncio
import os
import tempfile
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.broker import watchdog as watchdog


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, health_sequence, autotrading=None, autotrading_raises=False):
        self._seq = list(health_sequence)
        self._autotrading = autotrading or {"enabled": True, "method": "already_enabled"}
        self._autotrading_raises = autotrading_raises

    async def get_health(self):
        if not self._seq:
            return {"connected": True}
        return self._seq.pop(0)

    async def enable_autotrading(self):
        if self._autotrading_raises:
            raise RuntimeError("at check boom")
        return self._autotrading


def _fresh_state():
    return {"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}


async def _run_cycles(bridge, state, n, inhibit=False, start_bridge_launched=True):
    alerts = []

    async def fake_send(msg):
        alerts.append(msg)

    async def fake_start_bridge():
        return start_bridge_launched

    waits = []
    with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        for _ in range(n):
            waits.append(await watchdog.bridge_watchdog_check(
                bridge, state, inhibit, fake_start_bridge))
    return waits, alerts


def test_stays_connected_no_alert_check_interval(fresh_db):
    bridge = _FakeBridge([{"connected": True}])
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 1))
    assert waits == [60]
    assert alerts == []


def test_status_connected_alt_path_treated_as_connected(fresh_db):
    bridge = _FakeBridge([{"status": "connected"}])
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 1))
    assert waits == [60]


def test_get_health_raises_treated_as_disconnected(fresh_db):
    bridge = _FakeBridge([])

    async def raising_get_health():
        raise RuntimeError("http down")
    bridge.get_health = raising_get_health
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 1))
    assert waits == [60]


def test_single_fail_below_threshold_no_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}, {"connected": True}])
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 2))
    assert waits == [60, 60]
    assert alerts == []


def test_threshold_crossed_sends_offline_alert_once(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 2))
    assert alerts == ["MT5 bridge offline. Attempting automatic reconnect."]


def test_reconnect_autotrading_already_enabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": True, "method": "already_enabled"})
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 3))
    assert alerts[-1] == "MT5 bridge reconnected and healthy. Algo Trading was already active."


def test_reconnect_autotrading_freshly_enabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": True, "method": "fresh"})
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 3))
    assert alerts[-1] == "MT5 bridge reconnected and healthy. Algo Trading re-enabled automatically."


def test_reconnect_autotrading_still_disabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": False, "error": "terminal locked"})
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 3))
    assert alerts[-1] == ("MT5 bridge reconnected and healthy. Algo Trading is DISABLED — "
                           "re-enable the AutoTrading button in MT5 manually.")


def test_reconnect_autotrading_check_raises(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading_raises=True)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 3))
    assert alerts[-1] == "MT5 bridge reconnected and healthy. AutoTrading check failed: at check boom"


def test_restart_launched_returns_startup_wait(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 2, start_bridge_launched=True))
    assert waits == [60, 20]


def test_restart_launch_fails_falls_through_to_check_interval(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 2, start_bridge_launched=False))
    assert waits == [60, 60]


def test_inhibited_no_restart_no_offline_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 2, inhibit=True))
    assert waits == [60, 60]
    assert alerts == []


def test_cooldown_blocks_second_restart_no_duplicate_offline_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 10)
    state = _fresh_state()
    waits, alerts = asyncio.run(_run_cycles(bridge, state, 3))
    assert waits == [60, 20, 60]
    assert alerts == ["MT5 bridge offline. Attempting automatic reconnect."]


def test_cooldown_allows_second_restart_when_now_monotonic_advances(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 10)
    state = _fresh_state()

    async def fake_send(msg):
        pass

    async def fake_start_bridge():
        return True

    with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        asyncio.run(watchdog.bridge_watchdog_check(bridge, state, False, fake_start_bridge, now_monotonic=1000.0))
        asyncio.run(watchdog.bridge_watchdog_check(bridge, state, False, fake_start_bridge, now_monotonic=1000.0))
        # cooldown (180s) has elapsed by 1200.0
        w3 = asyncio.run(watchdog.bridge_watchdog_check(bridge, state, False, fake_start_bridge, now_monotonic=1200.0))
    assert state["last_restart_at"] == 1200.0
    assert w3 == 20
