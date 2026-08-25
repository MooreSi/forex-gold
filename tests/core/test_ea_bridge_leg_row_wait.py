"""A leg fill that arrives before open_trade()'s INSERT.

An anchor leg is a market order, so the EA fills and reports it before the
trade row exists -- the row only lands once the EA's parent ack returns, which
takes tens of seconds for a multi-leg template (10.5s observed live on
2026-07-30).

The first attempt at handling that waited inline, which stalled the EA reader
loop: _handle_conn awaits _dispatch directly and only refreshes _last_seen on
a read, so a 10s wait pushed is_ea_healthy() past its 8s timeout. Three
template activations then failed with "no healthy EA" and were retried.
The wait must happen off the reader loop.
"""
import asyncio
import time

import pytest

from backend.src.services.broker import ea_bridge


class _FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None


def _bridge():
    b = ea_bridge.EABridge(engine=None)
    b._writer = _FakeWriter()
    b._last_seen = time.time()
    return b


def test_wait_budget_exceeds_the_open_ack_cap():
    """The row cannot appear until the parent ack returns, and core_open_trade
    allows that up to 60s. A budget at or below the ack cap can never see the
    row it is waiting for."""
    assert ea_bridge._LEG_ROW_WAIT_S > 60.0


def test_wait_budget_is_longer_than_the_heartbeat_timeout():
    """Which is precisely why it must not run inline -- this test exists to
    make the coupling explicit if anyone shortens one of the two."""
    assert ea_bridge._LEG_ROW_WAIT_S > ea_bridge._HEARTBEAT_TIMEOUT_S


def _no_row(monkeypatch):
    """Make the row lookup report 'not there yet' without touching a DB."""
    from backend.src.db import database as db
    monkeypatch.setattr(db, "to_db_thread", lambda fn, *a, **kw: _coro(({}, False)))


def test_missing_row_returns_immediately_and_defers(monkeypatch):
    """The handler must hand the waiting to a task and return promptly, so the
    reader loop keeps draining and the EA stays healthy."""
    b = _bridge()
    monkeypatch.setattr(b, "_leg_position_volume", lambda ticket: _none())
    _no_row(monkeypatch)

    scheduled = {}

    async def _fake_defer(_apply, label, leg_trade_id, original_id, ticket, fill_price, lots):
        scheduled["called"] = True

    monkeypatch.setattr(b, "_promote_leg_when_row_exists", _fake_defer)

    async def _run():
        started = time.monotonic()
        await b._promote_leg_fill("abc123-a1", 111, 4050.0)
        return time.monotonic() - started

    elapsed = asyncio.run(_run())
    assert scheduled.get("called") is True
    assert elapsed < 1.0, "handler blocked the reader loop"


def test_ea_stays_healthy_while_a_leg_waits_for_its_row(monkeypatch):
    """The regression itself: with the wait deferred, health must survive well
    past the heartbeat timeout without a further EA message."""
    b = _bridge()
    monkeypatch.setattr(b, "_leg_position_volume", lambda ticket: _none())
    _no_row(monkeypatch)

    async def _slow_defer(*a, **kw):
        await asyncio.sleep(0.2)          # stands in for the real 75s budget

    monkeypatch.setattr(b, "_promote_leg_when_row_exists", _slow_defer)

    async def _run():
        await b._promote_leg_fill("abc123-a1", 111, 4050.0)
        # Reader-loop time has not been consumed, so a heartbeat older than
        # the timeout is the only thing that could make this unhealthy.
        return b.is_ea_healthy()

    assert asyncio.run(_run()) is True


def test_deferred_promotion_gives_up_after_the_budget(monkeypatch):
    """It must not wait forever -- TemplateRepair is the backstop."""
    monkeypatch.setattr(ea_bridge, "_LEG_ROW_WAIT_S", 1.0)
    b = _bridge()
    calls = {"n": 0}

    def _never_finds():
        calls["n"] += 1
        return ({}, False)

    from backend.src.db import database as db
    monkeypatch.setattr(db, "to_db_thread",
                        lambda fn, *a, **kw: _coro(fn(*a, **kw)))

    async def _run():
        await b._promote_leg_when_row_exists(
            _never_finds, "Anchor Leg 1", "abc123-a1", "abc123", 111, 4050.0, 0.03)

    asyncio.run(_run())
    assert calls["n"] >= 1


def _none():
    async def _inner():
        return None
    return _inner()


def _coro(value):
    async def _inner():
        return value
    return _inner()
