"""Proves backend.src.services.trading.profit_sync's extracted functions behave
identically to SimulationEngine's originals, characterized in
test_profit_sync_characterization.py -- see
docs/todo/refactor/core-profit-sync-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
verified via the fake bridge's own call log.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.trading import profit_sync as ps
from backend.src.services.trading.sim_account import get_sim_account


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


# ── sync_profit ───────────────────────────────────────────────────────────

def test_sync_profit_no_deals_returns_none(fresh_db):
    _insert_trade(mt5_profit=None)
    bridge = _FakeBridge(position_history=[])
    result = asyncio.run(ps.sync_profit("t-1", 555, bridge))
    assert result is None
    assert _trade_dict()["mt5_profit"] is None


def test_sync_profit_no_closing_entries_returns_none(fresh_db):
    _insert_trade(mt5_profit=None)
    bridge = _FakeBridge(position_history=[{"entry": 0, "profit": 0.0}])
    result = asyncio.run(ps.sync_profit("t-1", 555, bridge))
    assert result is None


def test_sync_profit_first_time_corrects_balance(fresh_db):
    _insert_trade(mt5_profit=None, net_pnl=40.0)
    starting_balance = get_sim_account()["balance"]
    bridge = _FakeBridge(position_history=[{"entry": 1, "profit": 50.0, "swap": -1.0, "fee": 0.0}])
    result = asyncio.run(ps.sync_profit("t-1", 555, bridge))
    assert result == 49.0
    assert get_sim_account()["balance"] == starting_balance + 9.0
    trade = _trade_dict()
    assert trade["mt5_profit"] == 49.0
    assert trade["net_pnl"] == 49.0


def test_sync_profit_already_synced_skips_balance_correction(fresh_db):
    _insert_trade(mt5_profit=30.0, net_pnl=40.0)
    starting_balance = get_sim_account()["balance"]
    bridge = _FakeBridge(position_history=[{"entry": 1, "profit": 50.0, "swap": 0.0, "fee": 0.0}])
    result = asyncio.run(ps.sync_profit("t-1", 555, bridge))
    assert result == 50.0
    assert get_sim_account()["balance"] == starting_balance
    assert _trade_dict()["mt5_profit"] == 50.0


def test_sync_profit_falls_back_to_deal_history_filtered_by_ticket(fresh_db):
    _insert_trade(mt5_profit=None, net_pnl=10.0)
    all_deals = [
        {"position_id": "555", "entry": 1, "profit": 20.0, "swap": 0.0, "fee": -1.0},
        {"position_id": "999", "entry": 1, "profit": 5.0},
    ]
    bridge = _FakeBridge(position_history=[], deal_history=all_deals)
    result = asyncio.run(ps.sync_profit("t-1", 555, bridge))
    assert result == 19.0


# ── schedule_profit_sync ──────────────────────────────────────────────────

def test_schedule_profit_sync_already_synced_returns_immediately(fresh_db):
    _insert_trade(mt5_profit=49.0)
    with mock.patch.object(ps, "sync_profit", new=mock.AsyncMock()) as sp:
        asyncio.run(ps.schedule_profit_sync("t-1", 555, _FakeBridge()))
    assert not sp.called


def test_schedule_profit_sync_succeeds_first_attempt_no_sleep(fresh_db):
    _insert_trade(mt5_profit=None)
    with mock.patch.object(ps, "sync_profit", new=mock.AsyncMock(return_value=49.0)) as sp, \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep_mock:
        asyncio.run(ps.schedule_profit_sync("t-1", 555, _FakeBridge()))
    assert sp.call_count == 1
    assert not sleep_mock.called


def test_schedule_profit_sync_retries_until_success(fresh_db):
    _insert_trade(mt5_profit=None)
    results = iter([None, None, 49.0])
    async def fake_sync(trade_id, ticket, bridge):
        return next(results)
    with mock.patch.object(ps, "sync_profit", side_effect=fake_sync) as sp, \
         mock.patch("asyncio.sleep", new=mock.AsyncMock()) as sleep_mock:
        asyncio.run(ps.schedule_profit_sync("t-1", 555, _FakeBridge()))
    assert sp.call_count == 3
    assert sleep_mock.call_count == 2


# ── profit_sweep ──────────────────────────────────────────────────────────

def test_profit_sweep_picks_up_null_and_recent_zero_skips_synced(fresh_db):
    _insert_trade("t-1", mt5_profit=None, close_time=time.time())
    _insert_trade("t-2", mt5_profit=0.0, close_time=time.time())
    _insert_trade("t-3", mt5_profit=5.0, close_time=time.time())
    calls = []
    async def fake_sync(trade_id, ticket, bridge):
        calls.append(trade_id)
        if trade_id == "t-2":
            raise RuntimeError("boom")
        return 1.0
    with mock.patch.object(ps, "sync_profit", side_effect=fake_sync):
        asyncio.run(ps.profit_sweep(_FakeBridge()))
    assert sorted(calls) == ["t-1", "t-2"]


