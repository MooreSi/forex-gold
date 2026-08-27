"""Periodic re-validation of resting pending limit orders
(core_pending_order_revalidation.py) -- a genuine broker-side pending order
has no fill-time gate at all (MT5 fills it directly, no round-trip back to
Python), so this is the only re-check it gets before either firing or
expiring. Covers: EA-unhealthy no-op, no-orders no-op, grace period, calm
market leaves the order alone, and an invalidated setup gets cancelled.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from tests.conftest import remove_db_file
from backend.src.services.positions import core_pending_order_revalidation as por


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    # The code under test reaches the database through
    # db_module.to_db_thread(broker_repo.fetch_working_pending_orders), which
    # caches a connection on the DB WORKER thread -- a different thread-local
    # from the caller's. Resetting only the caller's left that one open, which
    # is the single surviving handle the Windows probe reported.
    db._db_executor.submit(_reset_thread_local_connection).result()
    remove_db_file(path)


def _insert_working_order(conn, trade_id, direction, created_at, ticket=555):
    conn.execute(
        "INSERT INTO vantage_pending_orders (trade_id, signal_id, channel_name, direction, "
        "price, stop_loss, tps_json, pcts_json, be_at_pos, lot_size, ea_ticket, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", "Test Channel", direction, 2400.0, 2410.0,
         "{}", "[1.0]", 0, 0.10, ticket, "working", created_at),
    )


class _FakeBridge:
    def __init__(self, candles):
        self._candles = candles

    async def get_candles(self, timeframe, count):
        return self._candles


class _FakeEA:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.cancelled = []

    def is_ea_healthy(self):
        return self.healthy

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True


_CALM_CANDLE = [{"open": 2395.0, "high": 2396.0, "low": 2393.5, "close": 2394.5}]
_EXHAUSTED_SELL_CANDLE = [{"open": 2400.0, "high": 2401.0, "low": 2380.0, "close": 2381.0}]


def test_no_orders_makes_no_calls(fresh_db, monkeypatch):
    ea = _FakeEA()
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_CALM_CANDLE)))
    assert ea.cancelled == []


def test_ea_unhealthy_skips_entirely(fresh_db, monkeypatch):
    with db.db() as conn:
        _insert_working_order(conn, "t1", "SELL", time.time() - 1000)
    ea = _FakeEA(healthy=False)
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_EXHAUSTED_SELL_CANDLE)))
    assert ea.cancelled == []


def test_order_within_grace_period_not_checked(fresh_db, monkeypatch):
    with db.db() as conn:
        _insert_working_order(conn, "t1", "SELL", time.time())  # just placed
    ea = _FakeEA()
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    monkeypatch.setattr("backend.src.services.dpm.engine.compute_atr", lambda *a, **k: 8.0)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_EXHAUSTED_SELL_CANDLE)))
    assert ea.cancelled == []


def test_calm_market_leaves_order_resting(fresh_db, monkeypatch):
    with db.db() as conn:
        _insert_working_order(conn, "t1", "SELL", time.time() - 1000)
    ea = _FakeEA()
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    monkeypatch.setattr("backend.src.services.dpm.engine.compute_atr", lambda *a, **k: 8.0)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_CALM_CANDLE)))
    assert ea.cancelled == []


def test_invalidated_setup_cancels_order(fresh_db, monkeypatch):
    with db.db() as conn:
        _insert_working_order(conn, "t1", "SELL", time.time() - 1000, ticket=777)
    ea = _FakeEA()
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    monkeypatch.setattr("backend.src.services.dpm.engine.compute_atr", lambda *a, **k: 8.0)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_EXHAUSTED_SELL_CANDLE)))
    assert len(ea.cancelled) == 1
    trade_id, ticket, reason = ea.cancelled[0]
    assert trade_id == "t1"
    assert ticket == 777
    assert "revalidation" in reason


def test_no_ticket_recorded_skips_order(fresh_db, monkeypatch):
    with db.db() as conn:
        _insert_working_order(conn, "t1", "SELL", time.time() - 1000, ticket=None)
    ea = _FakeEA()
    monkeypatch.setattr("backend.src.services.broker.ea_bridge.get_instance", lambda: ea)
    monkeypatch.setattr("backend.src.services.dpm.engine.compute_atr", lambda *a, **k: 8.0)
    import asyncio
    asyncio.run(por.revalidate_pending_orders(_FakeBridge(_EXHAUSTED_SELL_CANDLE)))
    assert ea.cancelled == []
