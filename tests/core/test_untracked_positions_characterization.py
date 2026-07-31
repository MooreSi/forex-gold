"""Characterizes get_untracked_mt5_positions on SimulationEngine
(core/engine.py) before task 020 extracts it -- see
docs/todo/refactor/core-untracked-positions-migration/010-*.md.

Calls self._bridge.is_configured()/get_positions() and self.get_open_trades()
internally, so it needs a real SimulationEngine.__new__(SimulationEngine)
instance (correct method binding for get_open_trades) with _bridge set
manually to a fake test-double -- __init__ never runs, so no live MT5
bridge/HTTP client is ever constructed.
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, configured=True, positions=None, raises=False):
        self._configured = configured
        self._positions  = positions or []
        self._raises     = raises

    def is_configured(self) -> bool:
        return self._configured

    async def get_positions(self) -> list:
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._positions


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    return e


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, mt5_ticket, sig_id="sig-1", direction="BUY", status="open"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, 2400.0, 0.10, 0.10,
             2390.0, status, time.time()),
        )


def test_returns_empty_when_bridge_not_configured(fresh_db, engine):
    engine._bridge = _FakeBridge(configured=False, positions=[{"ticket": 1}])
    result = asyncio.run(SimulationEngine.get_untracked_mt5_positions(engine))
    assert result == []


def test_returns_empty_when_get_positions_raises(fresh_db, engine):
    engine._bridge = _FakeBridge(configured=True, raises=True)
    result = asyncio.run(SimulationEngine.get_untracked_mt5_positions(engine))
    assert result == []


def test_returns_empty_when_no_live_positions(fresh_db, engine):
    engine._bridge = _FakeBridge(configured=True, positions=[])
    result = asyncio.run(SimulationEngine.get_untracked_mt5_positions(engine))
    assert result == []


def test_returns_untracked_positions_tagged(fresh_db, engine):
    engine._bridge = _FakeBridge(configured=True, positions=[
        {"ticket": 111, "symbol": "XAUUSD"},
    ])
    result = asyncio.run(SimulationEngine.get_untracked_mt5_positions(engine))
    assert len(result) == 1
    assert result[0]["ticket"] == 111
    assert result[0]["_untracked"] is True


def test_excludes_positions_matching_a_tracked_trade(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=111)
    engine._bridge = _FakeBridge(configured=True, positions=[
        {"ticket": 111, "symbol": "XAUUSD"},
        {"ticket": 222, "symbol": "XAUUSD"},
    ])
    result = asyncio.run(SimulationEngine.get_untracked_mt5_positions(engine))
    tickets = [p["ticket"] for p in result]
    assert tickets == [222]
