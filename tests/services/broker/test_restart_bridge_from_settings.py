"""Settings > MT5 can restart the bridge (owner, 2026-09-29).

After bug 070 the Mac's bridge had to be restarted to serve /orders, and the
only way was Telegram's /restart_bridge. The React MT5 tab had no control.

The restart itself is the one every other path uses (`start_bridge_process`):
on the Mac it tears the Wine session down, terminal and EA included, and
starts it again; on the Windows in-process bridge it reconnects. What is new is
only how the result is judged: by the bridge's own health read, which answers
on whatever port or process the bridge has. The Telegram command's port-9000
wait is wrong on this Mac (9010) and meaningless in-process, and is left alone
here because a test pins its wording.

Nothing here reaches a broker or starts a process: `start` and `health` are
recorders, and the wait between polls is zeroed.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.api.routers import settings as settings_router
from backend.src.services.broker import bridge_process as bp


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(bp, "_RESTART_POLL_S", 0.0)


class _Engine:
    def __init__(self, launched=True, healths=None):
        self._launched = launched
        self._healths = list(healths or [{"connected": True, "trade_allowed": True}])
        self.starts = 0

    async def start_bridge_process(self):
        self.starts += 1
        return self._launched

    async def get_bridge_health(self):
        h = self._healths.pop(0) if len(self._healths) > 1 else self._healths[0]
        if isinstance(h, Exception):
            raise h
        return h


def _restart(eng):
    return asyncio.run(bp.restart_and_wait(eng))


def test_a_restart_that_reconnects_says_so():
    eng = _Engine()
    result = _restart(eng)
    assert eng.starts == 1
    assert result["ok"] is True
    assert "connected" in result["message"]


def test_it_waits_for_the_bridge_to_come_back():
    # Wine takes 15-30 s: the first reads fail while the new bridge boots.
    eng = _Engine(healths=[ConnectionError("refused"), {"connected": False},
                           {"connected": True, "trade_allowed": True}])
    assert _restart(eng)["ok"] is True


def test_a_launch_that_fails_says_nothing_was_restarted():
    result = _restart(_Engine(launched=False))
    assert result["ok"] is False
    assert "could not be started" in result["message"]


def test_a_bridge_that_never_connects_is_not_called_a_success():
    result = _restart(_Engine(healths=[{"connected": False}]))
    assert result["ok"] is False
    assert "not connected" in result["message"]


def test_algo_trading_off_is_said_out_loud():
    result = _restart(_Engine(healths=[{"connected": True, "trade_allowed": False}]))
    assert result["ok"] is True
    assert "Algo Trading is OFF" in result["message"]


def test_a_second_press_while_one_runs_does_not_restart_again():
    async def both():
        gate = asyncio.Event()
        eng = _Engine()

        async def slow_start():
            eng.starts += 1
            await gate.wait()
            return True

        eng.start_bridge_process = slow_start
        first = asyncio.create_task(bp.restart_and_wait(eng))
        await asyncio.sleep(0)
        second = await bp.restart_and_wait(eng)
        gate.set()
        await first
        return eng.starts, second

    starts, second = asyncio.run(both())
    assert starts == 1
    assert second["ok"] is False and "already" in second["message"]


def test_the_route_runs_the_engines_own_restart():
    eng = _Engine()
    result = asyncio.run(settings_router.restart_bridge(eng=eng))
    assert eng.starts == 1
    assert result["ok"] is True
