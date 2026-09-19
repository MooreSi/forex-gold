"""Stopping the app from the dashboard.

The NiceGUI header had a power button offering Restart and Stop. The React
port kept restart (`POST /api/node/restart`) and lost stop, so the only way to
shut the app down was to kill the process -- which skips the one thing the
restart path is careful about: persisting the Telegram bot's update offset, so
the next start does not replay the command that caused the shutdown.

Nothing here closes a position or touches the broker. Engines stop because the
process stops, and any open position keeps running to its own SL/TP on the
broker's side, exactly as it does when the machine is turned off.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.health import power


class _Engine:
    def __init__(self, offset=41, fail=False):
        self._bot_offset = offset
        self.fail = fail


@pytest.fixture
def recorder(monkeypatch):
    """Capture what would have been written and stopped, stopping nothing."""
    calls = {"offset": None, "stopped": 0}

    def _set(key, value):
        calls["offset"] = (key, value)

    def _stop():
        calls["stopped"] += 1
        return True

    monkeypatch.setattr(power, "_set_app_config", _set)
    monkeypatch.setattr(power, "_shutdown_ui", _stop)
    return calls


class TestStopping:

    @pytest.mark.asyncio
    async def test_it_persists_the_bot_offset_before_stopping(self, recorder):
        # Without this the restarted process replays the update that stopped
        # it. The restart path has always done it; stop did not exist to.
        await power.stop_app(_Engine(offset=41), delay_secs=0)
        await asyncio.sleep(0)

        assert recorder["offset"] == ("bot_update_offset", "41")

    @pytest.mark.asyncio
    async def test_it_asks_the_server_to_stop(self, recorder):
        await power.stop_app(_Engine(), delay_secs=0)
        await asyncio.sleep(0.01)

        assert recorder["stopped"] == 1

    @pytest.mark.asyncio
    async def test_the_reply_goes_out_before_the_server_goes_away(self, recorder):
        # The stop is scheduled, not awaited. A synchronous shutdown kills the
        # connection carrying the response, so the dashboard shows a network
        # error for an action that worked.
        await power.stop_app(_Engine(), delay_secs=5)

        assert recorder["stopped"] == 0

    @pytest.mark.asyncio
    async def test_it_says_what_is_happening_and_that_it_is_not_coming_back(
        self, recorder,
    ):
        message = await power.stop_app(_Engine(), delay_secs=0)

        assert "restart" not in message.lower() or "not" in message.lower()
        assert "stop" in message.lower() or "shut" in message.lower()

    @pytest.mark.asyncio
    async def test_an_offset_that_cannot_be_written_does_not_block_the_stop(
        self, recorder, monkeypatch,
    ):
        # The operator pressed stop. A failed bookkeeping write is a worse
        # reason to stay running than replaying one Telegram update is to
        # stop.
        def _boom(key, value):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(power, "_set_app_config", _boom)

        await power.stop_app(_Engine(), delay_secs=0)
        await asyncio.sleep(0.01)

        assert recorder["stopped"] == 1

    @pytest.mark.asyncio
    async def test_an_engine_with_no_offset_still_stops(self, recorder):
        class _Bare:
            pass

        await power.stop_app(_Bare(), delay_secs=0)
        await asyncio.sleep(0.01)

        assert recorder["stopped"] == 1
