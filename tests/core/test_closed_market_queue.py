"""Queued closed-market limit signals.

`core_closed_market_queue.py` was 24.2% covered -- 47 of its 62 statements
never executed -- and it is in `services/positions`, one of the three areas the
2026-08-25 merge pushed below its coverage floor.

The behaviour is worth pinning on its own merits. This module exists because
limit setups posted over a weekend used to be dropped silently, so every
failure mode here is "a signal the user expected to be placed quietly wasn't",
which nothing else would report.

Nothing here can reach a broker. The module's own docstring is explicit --
*"Nothing here places, closes or modifies an MT5 order; flushing hands the
stored dict back to handle_limit_order_signal, which owns that entirely"* --
and `place_fn` is injected, so the tests pass their own coroutine.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.src.services.positions import core_closed_market_queue as q


@pytest.fixture
def market_closed(monkeypatch):
    """The weekend. conftest's autouse _market_week_open pins this open for the
    whole suite, so a test about closed-market behaviour has to say so."""
    monkeypatch.setattr(q, "is_weekly_market_closed", lambda now=None: True)


@pytest.fixture
def market_open(monkeypatch):
    monkeypatch.setattr(q, "is_weekly_market_closed", lambda now=None: False)


def _parsed(**over):
    p = {"direction": "BUY", "symbol": "XAUUSD", "entry_low": 2390.0, "entry_high": 2395.0}
    p.update(over)
    return p


def _recorder():
    """A stand-in for handle_limit_order_signal that records its arguments."""
    seen = []

    async def place_fn(parsed, tg_id, channel_name, source_label):
        seen.append((tg_id, channel_name, source_label, parsed))
        return {"skip_reason": "placed"}

    return seen, place_fn


# ── should_queue ──────────────────────────────────────────────────────────────
#
# The distinction this module leads with: a genuine weekend close is "can't
# trade now" and holds the signal; a session toggle the user turned off is
# "don't trade now" and must keep dropping it.

def test_a_closed_market_queues_only_when_the_setting_is_on(market_closed):
    assert q.should_queue({"lk_queue_closed_market_limits": 1}) is True
    assert q.should_queue({"lk_queue_closed_market_limits": 0}) is False
    assert q.should_queue({}) is False


def test_an_open_market_never_queues_however_the_setting_is_set(market_open):
    """A session toggle being off is a deliberate choice, not an outage."""
    assert q.should_queue({"lk_queue_closed_market_limits": 1}) is False


# ── queue_closed_market_limit ─────────────────────────────────────────────────

def test_queueing_stores_the_signal_once(fresh_db):
    assert q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed()) is True

    rows = q.get_queued_limits()
    assert len(rows) == 1
    assert rows[0]["tg_message_id"] == "m-1"
    assert rows[0]["channel_name"] == "GD VIP"
    assert rows[0]["source_label"] == "vip"
    assert json.loads(rows[0]["parsed_json"])["entry_low"] == 2390.0


def test_requeueing_the_same_message_is_refused(fresh_db):
    """The buffered message is re-scanned every cycle. Without the INSERT OR
    IGNORE the same signal would pile up once a second until Monday."""
    assert q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed()) is True
    assert q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed()) is False
    assert len(q.get_queued_limits()) == 1


def test_the_queue_comes_back_oldest_first(fresh_db):
    """Replay order should match arrival order."""
    q.queue_closed_market_limit("m-2", "B", "b", _parsed())
    time.sleep(0.01)
    q.queue_closed_market_limit("m-1", "A", "a", _parsed())

    assert [r["tg_message_id"] for r in q.get_queued_limits()] == ["m-2", "m-1"]


# ── flush_queued_limits ───────────────────────────────────────────────────────

def test_nothing_is_flushed_while_the_market_is_still_closed(fresh_db, market_closed):
    q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed())
    seen, place_fn = _recorder()

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 0
    assert seen == []
    assert len(q.get_queued_limits()) == 1, "the signal must still be waiting"


def test_flushing_replays_each_signal_and_empties_the_queue(fresh_db, market_open):
    q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed())
    q.queue_closed_market_limit("m-2", "GD INST", "inst", _parsed(direction="SELL"))
    seen, place_fn = _recorder()

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 2
    assert [s[0] for s in seen] == ["m-1", "m-2"]
    assert seen[0][1:3] == ("GD VIP", "vip")
    assert seen[1][3]["direction"] == "SELL", "the stored parse must survive the round trip"
    assert q.get_queued_limits() == []


def test_flushing_an_empty_queue_does_nothing(fresh_db, market_open):
    seen, place_fn = _recorder()
    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 0
    assert seen == []


def test_a_signal_older_than_the_cutoff_expires_instead_of_being_placed(fresh_db, market_open):
    """Replaying a three-day-old limit into a market that has moved is worse
    than dropping it."""
    q.queue_closed_market_limit("m-old", "GD VIP", "vip", _parsed())
    with fresh_db.db() as conn:
        conn.execute(
            "UPDATE vantage_closed_market_queue SET queued_at=? WHERE tg_message_id='m-old'",
            (time.time() - q._MAX_QUEUE_AGE_SEC - 1,),
        )
    seen, place_fn = _recorder()

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 0
    assert seen == [], "an expired signal must not reach the placement path"
    assert q.get_queued_limits() == [], "and must not stay queued either"


def test_an_unreadable_parse_is_marked_failed_not_left_queued(fresh_db, market_open):
    """A row left 'queued' through a failure is retried every cycle for the
    rest of the week -- the module says so, so it is worth asserting."""
    q.queue_closed_market_limit("m-bad", "GD VIP", "vip", _parsed())
    with fresh_db.db() as conn:
        conn.execute("UPDATE vantage_closed_market_queue SET parsed_json='{not json'")
    seen, place_fn = _recorder()

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 0
    assert seen == []
    assert q.get_queued_limits() == []


def test_a_placement_that_raises_is_recorded_as_failed(fresh_db, market_open):
    """Same rule, the other failure path: the replay itself blowing up.

    Asserting the STATUS, not just absence from the queue. The row is marked
    'placed' before the attempt, so it drops out of get_queued_limits() either
    way -- an earlier version of this test passed happily with the failure
    handler deleted, which made it a test of the wrong line.
    """
    q.queue_closed_market_limit("m-1", "GD VIP", "vip", _parsed())

    async def place_fn(*a, **k):
        raise RuntimeError("bridge down")

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 0
    assert q.get_queued_limits() == [], "a failed replay must not be retried forever"

    with fresh_db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_closed_market_queue WHERE tg_message_id='m-1'"
        ).fetchone()[0]
    assert status == "failed", (
        "a replay that raised is recorded as 'placed' -- the queue is honest "
        "about expiry and bad JSON, and must be about this too"
    )


def test_one_bad_signal_does_not_stop_the_rest_of_the_queue(fresh_db, market_open):
    q.queue_closed_market_limit("m-bad", "A", "a", _parsed())
    q.queue_closed_market_limit("m-ok", "B", "b", _parsed())
    seen = []

    async def place_fn(parsed, tg_id, channel_name, source_label):
        if tg_id == "m-bad":
            raise RuntimeError("nope")
        seen.append(tg_id)
        return {}

    assert asyncio.run(q.flush_queued_limits({}, place_fn)) == 1
    assert seen == ["m-ok"]
