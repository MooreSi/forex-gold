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
from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


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

# ── _cmd_switch_env ───────────────────────────────────────────────────────

# ── _cmd_restart_app ──────────────────────────────────────────────────────

def test_restart_app_success(fresh_db, engine):
    engine._bot_offset = 12345
    with mock.patch("subprocess.Popen") as popen_mock, \
         mock.patch("backend.src.utils.os_utils.open_restart_log") as log_mock, \
         mock.patch("backend.src.utils.os_utils.delayed_relaunch_cmd", return_value=["fakecmd"]):
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(SimulationEngine._cmd_restart_app(engine, []))
    assert "Restarting app in 5 seconds" in result
    assert db.get_app_config("bot_update_offset") == "12345"
    assert popen_mock.called


def test_restart_app_popen_failure_caught(fresh_db, engine):
    with mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")), \
         mock.patch("backend.src.utils.os_utils.open_restart_log") as log_mock:
        log_mock.return_value.__enter__ = mock.Mock(return_value=mock.Mock())
        log_mock.return_value.__exit__ = mock.Mock(return_value=False)
        result = asyncio.run(SimulationEngine._cmd_restart_app(engine, []))
    assert "Restart failed: spawn failed" in result


# ── _cmd_headless ─────────────────────────────────────────────────────────

