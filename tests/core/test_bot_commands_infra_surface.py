"""Proves forex_trader.core.core_bot_commands_infra's extracted functions
behave identically to SimulationEngine's originals, characterized in
test_bot_commands_infra_characterization.py -- see
docs/todo/refactor/core-bot-commands-infra-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. No test ever touches a real file path, sends real credentials,
spawns a real subprocess, or force-exits the test process.
"""
import asyncio
import os
import tempfile
from unittest import mock

import pytest

import backend.src.config as cfg_mod
from forex_trader.core import database as db
from forex_trader.core import core_bot_commands_infra as cmds


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


_CREDS = {
    "login": "12345", "password_enc": "pw", "server": "Demo-Server",
    "live_login": "99999", "live_password_enc": "pw2", "live_server": "Live-Server",
}


# ── cmd_restart_bridge ────────────────────────────────────────────────────

def test_restart_bridge_launch_fails(fresh_db):
    start = mock.AsyncMock(return_value=False)
    result = asyncio.run(cmds.cmd_restart_bridge([], _FakeBridge(), start))
    assert "Could not start bridge" in result


def test_restart_bridge_port_never_binds(fresh_db):
    start = mock.AsyncMock(return_value=True)
    with mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=False), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(cmds.cmd_restart_bridge([], _FakeBridge(), start))
    assert "port 9000 not bound" in result


def test_restart_bridge_mt5_not_connected(fresh_db):
    start = mock.AsyncMock(return_value=True)
    bridge = _FakeBridge(health={"connected": False})
    with mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(cmds.cmd_restart_bridge([], bridge, start))
    assert "MT5 is not connected yet" in result


def test_restart_bridge_already_active(fresh_db):
    start = mock.AsyncMock(return_value=True)
    bridge = _FakeBridge(health={"connected": True, "trade_allowed": True})
    with mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(cmds.cmd_restart_bridge([], bridge, start))
    assert result == "Bridge restarted and connected. Algo Trading: active."


def test_restart_bridge_auto_enable_already_enabled(fresh_db):
    start = mock.AsyncMock(return_value=True)
    bridge = _FakeBridge(health={"connected": True, "trade_allowed": False},
                         autotrading={"enabled": True, "method": "already_enabled"})
    with mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(cmds.cmd_restart_bridge([], bridge, start))
    assert result == "Bridge restarted and connected. Algo Trading: active."


def test_restart_bridge_auto_enable_fails(fresh_db):
    start = mock.AsyncMock(return_value=True)
    bridge = _FakeBridge(health={"connected": True, "trade_allowed": False},
                         autotrading={"enabled": False, "error": "denied"})
    with mock.patch("forex_trader.core.platform_utils.is_port_listening", return_value=True), \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()):
        result = asyncio.run(cmds.cmd_restart_bridge([], bridge, start))
    assert "Algo Trading: DISABLED" in result
    assert "denied" in result


# ── cmd_switch_env ────────────────────────────────────────────────────────

def test_switch_env_no_credentials_configured(fresh_db):
    with mock.patch.object(db, "get_mt5_credentials", return_value={}), \
         mock.patch.object(db, "init") as init_mock:
        result = asyncio.run(cmds.cmd_switch_env("demo", _FakeBridge()))
    assert "credentials not configured" in result
    assert not init_mock.called


def test_switch_env_succeeds(fresh_db):
    bridge = _FakeBridge(send_result={"status": "connected"}, health={"trade_allowed": True})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init") as init_mock, \
         mock.patch.object(cfg_mod, "save_to_yaml") as save_mock, \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(cmds.cmd_switch_env("demo", bridge))
    assert "Switched to Demo account (login 12345)" in result
    assert init_mock.called
    assert "forex_trader_demo.db" in str(init_mock.call_args)
    save_mock.assert_called_once_with({"account_env": "demo"})
    assert bridge.send_credentials_calls == [(12345, "pw", "Demo-Server")]


def test_switch_env_bridge_error_status(fresh_db):
    bridge = _FakeBridge(send_result={"status": "error", "error": "bad creds"})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init"), mock.patch.object(cfg_mod, "save_to_yaml"), \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(cmds.cmd_switch_env("demo", bridge))
    assert "MT5 bridge returned: bad creds" in result
    assert "/restartbridge" in result


def test_switch_env_live_uses_live_credentials(fresh_db):
    bridge = _FakeBridge(send_result={"status": "connected"}, health={"trade_allowed": False})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init"), mock.patch.object(cfg_mod, "save_to_yaml"), \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(cmds.cmd_switch_live([], bridge))
    assert "Switched to Live account (login 99999)" in result
    assert bridge.send_credentials_calls == [(99999, "pw2", "Live-Server")]


def test_switch_demo_delegates_to_switch_env(fresh_db):
    bridge = _FakeBridge(send_result={"status": "connected"}, health={"trade_allowed": True})
    with mock.patch.object(db, "get_mt5_credentials", return_value=_CREDS), \
         mock.patch.object(db, "init"), mock.patch.object(cfg_mod, "save_to_yaml"), \
         mock.patch.object(db, "sync_bridge_credentials_file"):
        result = asyncio.run(cmds.cmd_switch_demo([], bridge))
    assert "Switched to Demo account (login 12345)" in result


# ── cmd_restart_app ───────────────────────────────────────────────────────

def test_restart_app_success(fresh_db):
    with mock.patch("subprocess.Popen") as popen_mock, \
         mock.patch("forex_trader.core.platform_utils.open_restart_log") as log_mock, \
         mock.patch("forex_trader.core.platform_utils.delayed_relaunch_cmd", return_value=["fakecmd"]):
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(cmds.cmd_restart_app([], 12345))
    assert "Restarting app in 5 seconds" in result
    assert db.get_app_config("bot_update_offset") == "12345"
    assert popen_mock.called


def test_restart_app_popen_failure_caught(fresh_db):
    with mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")), \
         mock.patch("forex_trader.core.platform_utils.open_restart_log") as log_mock:
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(cmds.cmd_restart_app([], 0))
    assert "Restart failed: spawn failed" in result


# ── cmd_headless ──────────────────────────────────────────────────────────

def test_headless_no_args_shows_usage_and_current_state(fresh_db):
    restart_app = mock.AsyncMock()
    result = asyncio.run(cmds.cmd_headless([], restart_app))
    assert "Usage: /headless on | off" in result
    assert "Currently: OFF" in result
    assert not restart_app.called


def test_headless_on_sets_flag_and_delegates(fresh_db):
    restart_app = mock.AsyncMock(return_value="Restarting app in 5 seconds — reconnect your browser shortly.")
    result = asyncio.run(cmds.cmd_headless(["on"], restart_app))
    assert "Headless mode enabled." in result
    assert "will not be available" in result
    assert db.get_app_config("headless_mode_enabled") == "1"


def test_headless_off_clears_flag_and_delegates(fresh_db):
    restart_app = mock.AsyncMock(return_value="Restarting app in 5 seconds — reconnect your browser shortly.")
    result = asyncio.run(cmds.cmd_headless(["off"], restart_app))
    assert "Headless mode disabled" in result
    assert db.get_app_config("headless_mode_enabled") == "0"
