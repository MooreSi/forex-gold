"""Characterizes the infrastructure Telegram bot commands on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-bot-commands-infra-migration/010-*.md.

_start_bridge_process/subprocess.Popen/db_module.init/config.save_to_yaml/
db_module.sync_bridge_credentials_file/bridge.send_credentials are all
mocked -- no test ever touches a real file path, sends real credentials,
spawns a real subprocess, or force-exits the test process.
"""
import asyncio
import os
import tempfile
from unittest import mock

import pytest

import backend.src.config as cfg_mod
from forex_trader.core import database as db
from forex_trader.core.engine import SimulationEngine


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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, health=None, autotrading=None, send_result=None):
        self._health = health or {"connected": True, "trade_allowed": True}
        self._autotrading = autotrading or {"enabled": True, "method": "clicked"}
        self._send_result = send_result or {"status": "connected"}
        self.send_credentials_calls = []

    async def get_health(self):
        return self._health

    async def enable_autotrading(self):
        return self._autotrading

    async def send_credentials(self, login, password, server):
        self.send_credentials_calls.append((login, password, server))
        return self._send_result


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._bot_offset = 0
    return e


_CREDS = {
    "login": "12345", "password_enc": "pw", "server": "Demo-Server",
    "live_login": "99999", "live_password_enc": "pw2", "live_server": "Live-Server",
}


# ── _cmd_restart_bridge ──────────────────────────────────────────────────

def test_restart_bridge_launch_fails(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=False)):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert "Could not start bridge" in result


def test_restart_bridge_port_never_binds(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=True)), \
         mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=False), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert "port 9000 not bound" in result


def test_restart_bridge_mt5_not_connected(fresh_db, engine):
    engine._bridge = _FakeBridge(health={"connected": False})
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=True)), \
         mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert "MT5 is not connected yet" in result


def test_restart_bridge_already_active(fresh_db, engine):
    engine._bridge = _FakeBridge(health={"connected": True, "trade_allowed": True})
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=True)), \
         mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert result == "Bridge restarted and connected. Algo Trading: active."


def test_restart_bridge_auto_enable_already_enabled(fresh_db, engine):
    engine._bridge = _FakeBridge(health={"connected": True, "trade_allowed": False},
                                 autotrading={"enabled": True, "method": "already_enabled"})
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=True)), \
         mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert result == "Bridge restarted and connected. Algo Trading: active."


def test_restart_bridge_auto_enable_fails(fresh_db, engine):
    engine._bridge = _FakeBridge(health={"connected": True, "trade_allowed": False},
                                 autotrading={"enabled": False, "error": "denied"})
    with mock.patch.object(SimulationEngine, "_start_bridge_process", new=mock.AsyncMock(return_value=True)), \
         mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(SimulationEngine._cmd_restart_bridge(engine, []))
    assert "Algo Trading: DISABLED" in result
    assert "denied" in result


# ── _cmd_switch_env ───────────────────────────────────────────────────────

def test_switch_env_no_credentials_configured(fresh_db, engine):
    with mock.patch.object(db, "get_mt5_credentials", return_value={}), \
         mock.patch.object(db, "init") as init_mock:
        result = asyncio.run(SimulationEngine._cmd_switch_env(engine, "demo"))
    assert "credentials not configured" in result
    assert not init_mock.called


def test_switch_env_succeeds(fresh_db, engine):
    engine._bridge = _FakeBridge(send_result={"status": "connected"}, health={"trade_allowed": True})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init") as init_mock, \
         mock.patch.object(cfg_mod, "save_to_yaml") as save_mock, \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(SimulationEngine._cmd_switch_env(engine, "demo"))
    assert "Switched to Demo account (login 12345)" in result
    assert init_mock.called
    assert "forex_trader_demo.db" in str(init_mock.call_args)
    save_mock.assert_called_once_with({"account_env": "demo"})
    assert engine._bridge.send_credentials_calls == [(12345, "pw", "Demo-Server")]


def test_switch_env_bridge_error_status(fresh_db, engine):
    engine._bridge = _FakeBridge(send_result={"status": "error", "error": "bad creds"})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init"), mock.patch.object(cfg_mod, "save_to_yaml"), \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(SimulationEngine._cmd_switch_env(engine, "demo"))
    assert "MT5 bridge returned: bad creds" in result
    assert "/restartbridge" in result


def test_switch_env_live_uses_live_credentials(fresh_db, engine):
    engine._bridge = _FakeBridge(send_result={"status": "connected"}, health={"trade_allowed": False})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init"), mock.patch.object(cfg_mod, "save_to_yaml"), \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(SimulationEngine._cmd_switch_env(engine, "live"))
    assert "Switched to Live account (login 99999)" in result
    assert engine._bridge.send_credentials_calls == [(99999, "pw2", "Live-Server")]


# ── _cmd_restart_app ──────────────────────────────────────────────────────

def test_restart_app_success(fresh_db, engine):
    engine._bot_offset = 12345
    with mock.patch("subprocess.Popen") as popen_mock, \
         mock.patch("forex_trader.core.platform_utils.open_restart_log") as log_mock, \
         mock.patch("forex_trader.core.platform_utils.delayed_relaunch_cmd", return_value=["fakecmd"]):
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(SimulationEngine._cmd_restart_app(engine, []))
    assert "Restarting app in 5 seconds" in result
    assert db.get_app_config("bot_update_offset") == "12345"
    assert popen_mock.called


def test_restart_app_popen_failure_caught(fresh_db, engine):
    with mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")), \
         mock.patch("forex_trader.core.platform_utils.open_restart_log") as log_mock:
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(SimulationEngine._cmd_restart_app(engine, []))
    assert "Restart failed: spawn failed" in result


# ── _cmd_headless ─────────────────────────────────────────────────────────

def test_headless_no_args_shows_usage_and_current_state(fresh_db, engine):
    result = asyncio.run(SimulationEngine._cmd_headless(engine, []))
    assert "Usage: /headless on | off" in result
    assert "Currently: OFF" in result


def test_headless_on_sets_flag_and_delegates(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_cmd_restart_app",
                           new=mock.AsyncMock(return_value="Restarting app in 5 seconds — reconnect your browser shortly.")):
        result = asyncio.run(SimulationEngine._cmd_headless(engine, ["on"]))
    assert "Headless mode enabled." in result
    assert "will not be available" in result
    assert db.get_app_config("headless_mode_enabled") == "1"


def test_headless_off_clears_flag_and_delegates(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_cmd_restart_app",
                           new=mock.AsyncMock(return_value="Restarting app in 5 seconds — reconnect your browser shortly.")):
        result = asyncio.run(SimulationEngine._cmd_headless(engine, ["off"]))
    assert "Headless mode disabled" in result
    assert db.get_app_config("headless_mode_enabled") == "0"
