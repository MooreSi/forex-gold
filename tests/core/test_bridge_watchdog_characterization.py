"""Characterizes SimulationEngine._bridge_watchdog_loop's single-cycle
health check (core/engine.py) against UNMODIFIED engine.py, ahead of
extraction into forex_trader/core/core_bridge_watchdog.py -- see
docs/todo/refactor/core-bridge-watchdog-migration/010-*.md.

No MT5 order is ever placed, closed, or modified -- this function only
reads bridge health and toggles AutoTrading / restarts the bridge process.
"""
import asyncio
import os
import tempfile
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.runtime import TradingRuntime


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


def _make_engine(bridge, inhibit=False):
    e = TradingRuntime.__new__(TradingRuntime)
    e._monitor_running = True
    e._bridge = bridge
    e._bridge_inhibit_reconnect = inhibit
    return e


def _logging_sleep(engine, sleep_calls, n_sleeps):
    async def _sleep(*a, **k):
        sleep_calls.append(a[0] if a else None)
        if len(sleep_calls) >= n_sleeps:
            engine._monitor_running = False
    return _sleep


def _run(engine, n_sleeps, start_bridge_launched=True):
    sleep_calls = []
    alerts = []

    async def fake_send(msg):
        alerts.append(msg)

    async def fake_start_bridge(self_):
        return start_bridge_launched

    with mock.patch("asyncio.sleep", side_effect=_logging_sleep(engine, sleep_calls, n_sleeps)), \
         mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send), \
         mock.patch.object(TradingRuntime, "start_bridge_process", fake_start_bridge):
        asyncio.run(engine._bridge_watchdog_loop())
    return sleep_calls, alerts


def test_stays_connected_no_alert_check_interval(fresh_db):
    bridge = _FakeBridge([{"connected": True}])
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 2)
    assert sleep_calls == [180, 60]
    assert alerts == []


def test_status_connected_alt_path_treated_as_connected(fresh_db):
    bridge = _FakeBridge([{"status": "connected"}])
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 2)
    assert sleep_calls == [180, 60]
    assert alerts == []


def test_get_health_raises_treated_as_disconnected(fresh_db):
    bridge = _FakeBridge([])

    async def raising_get_health():
        raise RuntimeError("http down")
    bridge.get_health = raising_get_health
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 2)
    assert sleep_calls == [180, 60]


def test_single_fail_below_threshold_no_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}, {"connected": True}])
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 3)
    assert sleep_calls == [180, 60, 60]
    assert alerts == []


def test_threshold_crossed_sends_offline_alert_once(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 3)
    assert alerts == ["MT5 bridge offline. Attempting automatic reconnect."]


def test_reconnect_autotrading_already_enabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": True, "method": "already_enabled"})
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 4)
    assert alerts[-1] == "MT5 bridge reconnected and healthy. Algo Trading was already active."


def test_reconnect_autotrading_freshly_enabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": True, "method": "fresh"})
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 4)
    assert alerts[-1] == "MT5 bridge reconnected and healthy. Algo Trading re-enabled automatically."


def test_reconnect_autotrading_still_disabled(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading={"enabled": False, "error": "terminal locked"})
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 4)
    assert alerts[-1] == ("MT5 bridge reconnected and healthy. Algo Trading is DISABLED — "
                           "re-enable the AutoTrading button in MT5 manually.")


def test_reconnect_autotrading_check_raises(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 2 + [{"connected": True}],
                         autotrading_raises=True)
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 4)
    assert alerts[-1] == "MT5 bridge reconnected and healthy. AutoTrading check failed: at check boom"


def test_restart_launched_sleeps_startup_wait(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 3, start_bridge_launched=True)
    assert sleep_calls == [180, 60, 20]


def test_restart_launch_fails_falls_through_to_check_interval(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 3, start_bridge_launched=False)
    assert sleep_calls == [180, 60, 60]


def test_inhibited_no_restart_no_offline_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 5)
    e = _make_engine(bridge, inhibit=True)
    sleep_calls, alerts = _run(e, 3)
    assert sleep_calls == [180, 60, 60]
    assert alerts == []


def test_cooldown_blocks_second_restart_no_duplicate_offline_alert(fresh_db):
    bridge = _FakeBridge([{"connected": False}] * 10)
    e = _make_engine(bridge)
    sleep_calls, alerts = _run(e, 4)
    # 180 startup, 60 (1 fail), 20 (restart launched), 60 (still down, cooldown blocks 2nd restart)
    assert sleep_calls == [180, 60, 20, 60]
    assert alerts == ["MT5 bridge offline. Attempting automatic reconnect."]
