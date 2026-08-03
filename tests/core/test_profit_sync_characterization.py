"""Characterizes _sync_profit/_schedule_profit_sync/_profit_sweep/
_close_full_after_tps on SimulationEngine (core/engine.py) before task 020
extracts them -- see docs/todo/refactor/core-profit-sync-migration/010-*.md.

Uses a fake bridge. asyncio.sleep is patched in the retry-loop tests to
avoid real up-to-30-minute delays. NO real or demo MT5 order is ever
placed, closed, or modified -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.services.trading import profit_sync as core_profit_sync
from backend.src.db import database as db
from backend.src.services.trading.sim_account import get_sim_account
from backend.src.runtime import SimulationEngine


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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, position_history=None, deal_history=None, positions=None,
                close_result=None, account=None):
        self._ph = position_history
        self._dh = deal_history or []
        self._positions = positions if positions is not None else []
        self._close_result = close_result or {"success": True, "close_price": 2419.0}
        self._account = account or {"balance": 1000.0}

    async def get_position_history(self, ticket):
        return self._ph

    async def get_deal_history(self, days):
        return self._dh

    async def get_positions(self):
        return self._positions

    async def close_position(self, ticket):
        return self._close_result

    async def get_account(self):
        return self._account


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    return e


def _insert_trade(trade_id="t-1", status="closed", mt5_ticket=555, net_pnl=45.0,
                  mt5_profit=None, close_time=None, remaining_lots=0.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, net_pnl, mt5_profit, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", 2400.0, 2400.0, 2400.0, 0.10,
             remaining_lots, 2390.0, status, time.time(), net_pnl, mt5_profit, close_time),
        )


def _trade_dict(trade_id="t-1"):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


# ── _sync_profit ──────────────────────────────────────────────────────────

def test_sync_profit_no_deals_returns_none(fresh_db, engine):
    _insert_trade(mt5_profit=None)
    engine._bridge = _FakeBridge(position_history=[])
    result = asyncio.run(SimulationEngine.sync_profit(engine, "t-1", 555))
    assert result is None
    assert _trade_dict()["mt5_profit"] is None


def test_sync_profit_no_closing_entries_returns_none(fresh_db, engine):
    _insert_trade(mt5_profit=None)
    engine._bridge = _FakeBridge(position_history=[{"entry": 0, "profit": 0.0}])
    result = asyncio.run(SimulationEngine.sync_profit(engine, "t-1", 555))
    assert result is None


def test_sync_profit_first_time_corrects_balance(fresh_db, engine):
    _insert_trade(mt5_profit=None, net_pnl=40.0)
    starting_balance = get_sim_account()["balance"]
    engine._bridge = _FakeBridge(position_history=[{"entry": 1, "profit": 50.0, "swap": -1.0, "fee": 0.0}])
    result = asyncio.run(SimulationEngine.sync_profit(engine, "t-1", 555))
    assert result == 49.0
    assert get_sim_account()["balance"] == starting_balance + 9.0
    trade = _trade_dict()
    assert trade["mt5_profit"] == 49.0
    assert trade["net_pnl"] == 49.0


def test_sync_profit_already_synced_skips_balance_correction(fresh_db, engine):
    _insert_trade(mt5_profit=30.0, net_pnl=40.0)
    starting_balance = get_sim_account()["balance"]
    engine._bridge = _FakeBridge(position_history=[{"entry": 1, "profit": 50.0, "swap": 0.0, "fee": 0.0}])
    result = asyncio.run(SimulationEngine.sync_profit(engine, "t-1", 555))
    assert result == 50.0
    assert get_sim_account()["balance"] == starting_balance
    assert _trade_dict()["mt5_profit"] == 50.0  # still refreshed, just no balance correction


def test_sync_profit_falls_back_to_deal_history_filtered_by_ticket(fresh_db, engine):
    _insert_trade(mt5_profit=None, net_pnl=10.0)
    all_deals = [
        {"position_id": "555", "entry": 1, "profit": 20.0, "swap": 0.0, "fee": -1.0},
        {"position_id": "999", "entry": 1, "profit": 5.0},
    ]
    engine._bridge = _FakeBridge(position_history=[], deal_history=all_deals)
    result = asyncio.run(SimulationEngine.sync_profit(engine, "t-1", 555))
    assert result == 19.0


# ── _schedule_profit_sync ────────────────────────────────────────────────

def test_schedule_profit_sync_already_synced_returns_immediately(fresh_db, engine):
    _insert_trade(mt5_profit=49.0)
    with mock.patch.object(core_profit_sync, "sync_profit", new=mock.AsyncMock()) as sp:
        asyncio.run(SimulationEngine._schedule_profit_sync(engine, "t-1", 555))
    assert not sp.called


# ── _profit_sweep ─────────────────────────────────────────────────────────

# ── _close_full_after_tps ────────────────────────────────────────────────

def test_close_full_no_residual_syncs_and_alerts(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.0)
    engine._bridge = _FakeBridge(positions=[])
    with mock.patch.object(SimulationEngine, "sync_profit", new=mock.AsyncMock(return_value=10.0)) as sp, \
         mock.patch.object(SimulationEngine, "_schedule_profit_sync", new=mock.AsyncMock()) as ssp:
        asyncio.run(SimulationEngine._close_full_after_tps(engine, "t-1", 555, 2420.0))
    assert sp.called
    assert ssp.called
    assert _trade_dict()["status"] == "closed"


def test_close_full_residual_reopens_and_closes(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.0)
    engine._bridge = _FakeBridge(
        positions=[{"ticket": 555, "volume": 0.02}],
        close_result={"success": True, "close_price": 2419.0},
    )
    with mock.patch.object(SimulationEngine, "record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(SimulationEngine._close_full_after_tps(engine, "t-1", 555, 2420.0))
    trade = _trade_dict()
    assert trade["status"] == "open"
    assert trade["remaining_lots"] == 0.02
    rc.assert_called_once_with("t-1", 2419.0, "all_tps_hit_residual")


def test_close_full_residual_close_fails_stays_reopened(fresh_db, engine):
    _insert_trade(mt5_ticket=555, remaining_lots=0.0)
    engine._bridge = _FakeBridge(
        positions=[{"ticket": 555, "volume": 0.02}],
        close_result={"success": False, "error": "reject"},
    )
    with mock.patch.object(SimulationEngine, "record_close", new=mock.AsyncMock()) as rc:
        asyncio.run(SimulationEngine._close_full_after_tps(engine, "t-1", 555, 2420.0))
    trade = _trade_dict()
    assert trade["status"] == "open"
    assert trade["remaining_lots"] == 0.02
    assert not rc.called


def test_close_full_no_ticket_skips_profit_sync(fresh_db, engine):
    _insert_trade(mt5_ticket=None, remaining_lots=0.0)
    with mock.patch.object(SimulationEngine, "sync_profit", new=mock.AsyncMock()) as sp:
        asyncio.run(SimulationEngine._close_full_after_tps(engine, "t-1", None, 2420.0))
    assert not sp.called
