"""PendingWatcher's cap on repeated activation attempts.

Before 2026-07-30 a signal whose activation kept failing was re-attempted
every 20s until it expired -- up to ~180 attempts on a 1h template window.
That is only safe if an attempt is free, and it is not: a grid template
stages real broker legs before the failure is even detectable. Five signals
produced ~133 activations and 36 untracked live positions.

The specific cause (an EA ack timeout) is fixed in core_open_trade; this cap
is what makes any FUTURE failure mode cost a bounded number of orders.
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.services.signals import pending_activation as pa
from backend.src.db import database as db


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
    pa._ACTIVATION_FAILURES.clear()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", "Reversal Engine", "SELL", 4063.0, 4066.0, 4071.5,
             "pending", time.time()),
        )
    yield db
    pa._ACTIVATION_FAILURES.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _Tick:
    ask = 4064.2
    bid = 4064.0
    spread_points = 20.0


def _signal_status():
    with db.db() as conn:
        return conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='sig-1'"
        ).fetchone()[0]


def _run_cycles(monkeypatch, error: Exception, cycles: int) -> int:
    """Drive the watcher `cycles` times with activation always failing.
    Returns how many activation attempts were actually made."""
    attempts = {"n": 0}

    async def _boom(*a, **kw):
        attempts["n"] += 1
        raise error

    monkeypatch.setattr(pa, "open_trade_from_signal", _boom)
    monkeypatch.setattr(pa, "price_in_entry_range", lambda *a, **kw: True)
    monkeypatch.setattr(pa.telegram_alerts, "send_message",
                        lambda *a, **kw: asyncio.sleep(0))

    retry_after: dict = {}
    rs = {"max_open_trades": 10, "trade_strategy": "scale_out"}
    for _ in range(cycles):
        retry_after.clear()          # simulate the 20s backoff having elapsed
        asyncio.run(pa.try_activate_pending_signals(
            _Tick(), rs, object(), retry_after, []))
    return attempts["n"]


def test_repeated_failures_stop_after_the_cap(monkeypatch, fresh_db):
    """The whole point: a signal that keeps failing must not keep placing
    orders. Ten cycles must not mean ten attempts."""
    attempts = _run_cycles(monkeypatch, RuntimeError("EA exploded"), cycles=10)
    assert attempts == pa._MAX_ACTIVATION_ATTEMPTS


def test_abandoned_signal_is_expired_not_left_pending(monkeypatch, fresh_db):
    _run_cycles(monkeypatch, RuntimeError("EA exploded"), cycles=10)
    assert _signal_status() == "expired"


def test_expected_refusals_do_not_burn_the_attempt_budget(monkeypatch, fresh_db):
    """A circuit breaker or a stood-down node costs nothing at the broker and
    clears on its own -- those must stay retryable indefinitely, or a 5-minute
    breaker would permanently kill every queued signal."""
    attempts = _run_cycles(
        monkeypatch,
        ValueError("Trade blocked — circuit breaker active. Resumes in ~5 min."),
        cycles=6)
    assert attempts == 6
    assert _signal_status() == "pending"


def test_pause_is_treated_as_an_expected_refusal(monkeypatch, fresh_db):
    attempts = _run_cycles(
        monkeypatch, ValueError("Trading paused until 14:14 — MT5 order blocked."),
        cycles=5)
    assert attempts == 5
    assert _signal_status() == "pending"


def test_a_success_clears_the_failure_count(monkeypatch, fresh_db):
    """Two failures then a success must not leave the signal one strike from
    being abandoned next time it is used."""
    calls = {"n": 0}

    async def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        return {"trade_id": "t1", "entry_price": 4064.0}

    monkeypatch.setattr(pa, "open_trade_from_signal", _flaky)
    monkeypatch.setattr(pa, "price_in_entry_range", lambda *a, **kw: True)
    monkeypatch.setattr(pa.telegram_alerts, "send_message",
                        lambda *a, **kw: asyncio.sleep(0))

    retry_after: dict = {}
    rs = {"max_open_trades": 10, "trade_strategy": "scale_out"}
    for _ in range(3):
        retry_after.clear()
        asyncio.run(pa.try_activate_pending_signals(
            _Tick(), rs, object(), retry_after, []))

    assert calls["n"] == 3
    assert "sig-1" not in pa._ACTIVATION_FAILURES
