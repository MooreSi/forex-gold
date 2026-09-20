"""The header's update badge reads a CACHED check, never a live one.

The header polls `/api/system/header` every five seconds. `check_for_update`
runs `git fetch` against GitHub, which takes as long as the network takes --
putting it on that path would mean a fetch every five seconds and a header
that stalls whenever the connection is slow.

So the badge asks `cached_update_check()`, which answers immediately with what
was last found and refreshes itself in the background at most every ten
minutes. These tests pin the two things that can go wrong: answering stale
forever, and starting a new fetch on every call.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_app_update as upd


@pytest.fixture
def checks(monkeypatch):
    """A scripted `check_for_update` that records how often it ran."""
    state = {"calls": 0, "result": {"available": True, "commits": [], "error": None},
             "block": None}

    async def _check():
        state["calls"] += 1
        if state["block"] is not None:
            await state["block"]
        return dict(state["result"])

    monkeypatch.setattr(upd, "check_for_update", _check)
    upd.reset_update_cache()
    return state


def test_the_first_call_answers_without_waiting_for_the_fetch(checks):
    """The point of the cache: the caller is never blocked on git."""
    async def _go():
        checks["block"] = asyncio.get_running_loop().create_future()
        first = upd.cached_update_check()
        # The fetch has not finished -- it cannot have, nothing resolved it.
        assert first.get("available") is not True
        checks["block"].set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(_go())


def test_the_answer_arrives_once_the_background_check_finishes(checks):
    async def _go():
        upd.cached_update_check()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return upd.cached_update_check()

    assert asyncio.run(_go())["available"] is True


def test_a_second_call_does_not_start_a_second_fetch(checks):
    """Polled every five seconds. One fetch per call is the bug this exists
    to avoid."""
    async def _go():
        for _ in range(5):
            upd.cached_update_check()
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        for _ in range(5):
            upd.cached_update_check()

    asyncio.run(_go())

    assert checks["calls"] == 1


def test_it_refetches_once_the_answer_is_old(checks, monkeypatch):
    async def _go():
        upd.cached_update_check()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Ten minutes later.
        now = upd._monotonic() + upd.UPDATE_CHECK_TTL_SECS + 1
        monkeypatch.setattr(upd, "_monotonic", lambda: now)
        upd.cached_update_check()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())

    assert checks["calls"] == 2


def test_a_failing_check_does_not_refetch_on_every_call(checks, monkeypatch):
    """A machine with no network would otherwise run a git fetch every five
    seconds, forever."""
    async def _boom():
        checks["calls"] += 1
        raise RuntimeError("no network")

    monkeypatch.setattr(upd, "check_for_update", _boom)

    async def _go():
        for _ in range(4):
            upd.cached_update_check()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(_go())

    assert checks["calls"] == 1


def test_a_check_that_raises_leaves_the_badge_off_rather_than_erroring(
    checks, monkeypatch,
):
    async def _boom():
        raise RuntimeError("no network")

    monkeypatch.setattr(upd, "check_for_update", _boom)

    async def _go():
        upd.cached_update_check()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return upd.cached_update_check()

    assert asyncio.run(_go()).get("available") is not True


def test_outside_a_running_loop_it_answers_rather_than_raising(checks):
    """`cached_update_check` is called from request handlers, but nothing
    stops a synchronous caller (a test, a CLI) from asking."""
    assert upd.cached_update_check().get("available") is not True
