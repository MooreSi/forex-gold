"""The VPS heartbeat reads its git commit on a worker thread, not the loop.

`heartbeat_fields()` and `running_commit()` shell out to `git rev-parse`.
The heartbeat runs every 3 s ON the event loop, so each beat spawned git
there; on the Windows VPS one spawn stalled the loop for 4,860 ms on
2026-09-28 20:39 (sampled in subprocess._execute_child <- get_commit_report
<- heartbeat_fields <- _status_payload). The EA link, the monitor loop and
every order path wait on the same loop.
"""
from __future__ import annotations

import threading

import pytest

from backend.src.services.cluster.sync import _telemetry as tel

pytestmark = pytest.mark.asyncio


class _Node(tel.TelemetryMixin):
    _main_engine = object()          # not None: the full payload is built

    def _sub_engines(self):
        return {}


@pytest.fixture
def git_threads(monkeypatch):
    seen: dict = {}

    def _fields():
        seen["heartbeat_fields"] = threading.current_thread()
        return {"commit": "a" * 40, "git_version": "2.55"}

    def _running():
        seen["running_commit"] = threading.current_thread()
        return "a" * 40

    monkeypatch.setattr(tel._update_sync, "heartbeat_fields", _fields)
    monkeypatch.setattr(tel._update_sync, "running_commit", _running)

    async def _status():
        return None
    monkeypatch.setattr(tel, "_trading_status", _status)
    monkeypatch.setattr(tel.db_module, "get_active_trader", lambda: "remote_vps")
    return seen


async def test_git_is_never_read_on_the_loop_thread(git_threads):
    loop_thread = threading.current_thread()

    payload = await _Node()._status_payload()

    assert payload["commit"] == "a" * 40
    assert payload["running_commit"] == "a" * 40
    assert git_threads["heartbeat_fields"] is not loop_thread
    assert git_threads["running_commit"] is not loop_thread
