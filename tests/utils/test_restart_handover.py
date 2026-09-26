"""A restart must outwait the app it replaces, however long that takes to stop.

Reported 2026-09-26: a restart during a model call left nothing running.
restart.log shows the old process stopping gracefully, which means uvicorn
waiting for in-flight requests to finish -- a DeepSeek analysis still being
written. That took about 20 seconds. The relaunch started after 5, waited its
15 for the single-instance lock, gave up with "FOREX Trader is already
running", and exited two seconds before the old process let go.

A relaunch now says it is one (`--handover`), and a handover waits minutes, not
seconds. A launch that is NOT a handover still refuses quickly, because the
usual reason for that is a second checkout started by mistake.

Nothing here starts a process: Popen is replaced by a recorder.
"""
from __future__ import annotations

import asyncio

import pytest

import run
from backend.src.utils import os_utils, single_instance


@pytest.fixture
def waits(monkeypatch):
    seen = []
    monkeypatch.setattr(single_instance, "acquire", lambda timeout=0.0: seen.append(timeout))
    return seen


class TestHowLongTheLockIsWaitedFor:
    def test_a_handover_waits_longer_than_a_slow_graceful_shutdown(self, monkeypatch, waits):
        monkeypatch.setattr(run.sys, "argv", ["run.py", "--no-browser", os_utils.HANDOVER_FLAG])

        assert run._claim_single_instance() is True
        # 20s is what the owner's app actually took; a model call can run for
        # minutes, so the margin is deliberately wide.
        assert waits[0] >= 120

    def test_an_ordinary_launch_still_refuses_quickly(self, monkeypatch, waits):
        monkeypatch.setattr(run.sys, "argv", ["run.py", "--no-browser"])

        run._claim_single_instance()

        assert waits[0] == run._SINGLE_INSTANCE_WAIT < 120


class TestEveryGracefulRestartSaysItIsAHandover:
    """The two restart paths that stop the server gracefully, and so can be
    held up by a request in flight."""

    @pytest.fixture
    def spawned(self, monkeypatch):
        seen = []
        monkeypatch.setattr(os_utils.sys, "platform", "darwin")
        monkeypatch.delenv("FOREX_LAUNCHER", raising=False)
        monkeypatch.setattr(os_utils.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
        return seen

    def test_the_power_button_and_the_updater(self, spawned, monkeypatch, tmp_path):
        monkeypatch.setattr(os_utils, "shutdown_ui", lambda: True)

        os_utils.restart_app(tmp_path)

        assert os_utils.HANDOVER_FLAG in spawned[0][-1]

    def test_telegram_restartapp_and_the_environment_switch(self, spawned, monkeypatch):
        from backend.src.services.telegram import bot_infra
        monkeypatch.setattr(bot_infra.subprocess, "Popen", lambda cmd, **k: spawned.append(cmd))
        monkeypatch.setattr(bot_infra.db_module, "set_app_config", lambda *a: None)

        async def _no_shutdown(delay):
            return None

        monkeypatch.setattr(bot_infra, "_delayed_app_shutdown", _no_shutdown)

        async def _go():
            await bot_infra.cmd_restart_app([], 0)
            await asyncio.sleep(0)

        asyncio.run(_go())

        assert os_utils.HANDOVER_FLAG in spawned[0][-1]
