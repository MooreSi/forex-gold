"""Characterizes get_total_deposits/compute_mt5_performance/import_mt5_history
on SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-mt5-history-migration/010-*.md.

All three call self._bridge (get_account/get_deal_history/get_positions),
so they need a real SimulationEngine.__new__(SimulationEngine) instance
with _bridge set manually to a fake test-double -- __init__ never runs, so
no live MT5 bridge/HTTP client is ever constructed. import_mt5_history also
calls self.pnl(...) (pack 1's already-extracted math, unchanged on
engine.py too), which needs no self state.
"""
import asyncio
import json
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
    def __init__(self, account=None, deals=None, positions=None, raises=False):
        self._account   = account or {"balance": 1000.0, "equity": 1000.0}
        self._deals     = deals or []
        self._positions = positions or []
        self._raises    = raises
        self.deal_history_calls = 0

    async def get_account(self):
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._account

    async def get_deal_history(self, days: int = 7):
        self.deal_history_calls += 1
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._deals

    async def get_positions(self):
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._positions


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    return e


def _deal(position_id=None, profit=0.0, entry=0, ts=None, volume=0.10,
          deal_type=0, price=2400.0, swap=0.0, fee=0.0, comment=""):
    return {
        "position_id": position_id, "profit": profit, "entry": entry,
        "time": ts if ts is not None else time.time(), "volume": volume,
        "type": deal_type, "price": price, "swap": swap, "fee": fee, "comment": comment,
    }


# ── get_total_deposits ────────────────────────────────────────────────────────

def test_get_total_deposits_sums_deposit_only_deals(fresh_db, engine):
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=None, profit=500.0),   # deposit
        _deal(position_id=None, profit=-100.0),  # withdrawal
        _deal(position_id=42, profit=25.0),       # a real trade deal -- excluded
    ])
    total = asyncio.run(SimulationEngine.get_total_deposits(engine))
    assert total == 400.0


def test_get_total_deposits_caches_for_an_hour(fresh_db, engine):
    bridge = _FakeBridge(deals=[_deal(position_id=None, profit=500.0)])
    engine._bridge = bridge
    first = asyncio.run(SimulationEngine.get_total_deposits(engine))
    assert first == 500.0
    assert bridge.deal_history_calls == 1

    second = asyncio.run(SimulationEngine.get_total_deposits(engine))
    assert second == 500.0
    assert bridge.deal_history_calls == 1  # cache hit -- bridge not called again


def test_get_total_deposits_returns_zero_on_bridge_error(fresh_db, engine):
    engine._bridge = _FakeBridge(raises=True)
    total = asyncio.run(SimulationEngine.get_total_deposits(engine))
    assert total == 0.0


# ── compute_mt5_performance ────────────────────────────────────────────────────

def test_compute_mt5_performance_win_rate_and_pnl(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(
        account={"balance": 1000.0, "equity": 1050.0},
        deals=[
            _deal(position_id=1, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=1, entry=1, ts=now - 1800, profit=100.0),
            _deal(position_id=2, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=2, entry=1, ts=now - 1800, profit=-50.0),
        ],
        positions=[],
    )
    perf = asyncio.run(SimulationEngine.compute_mt5_performance(engine, days=90))
    assert perf["closed_trades"] == 2
    assert perf["win_rate_pct"] == 50.0
    assert perf["source"] == "mt5"
    assert perf["balance"] == 1000.0
    assert perf["equity"] == 1050.0


def test_compute_mt5_performance_includes_open_positions_pnl(fresh_db, engine):
    engine._bridge = _FakeBridge(
        account={"balance": 1000.0, "equity": 1000.0},
        deals=[],
        positions=[{"profit": 30.0}, {"profit": -10.0}],
    )
    perf = asyncio.run(SimulationEngine.compute_mt5_performance(engine, days=90))
    assert perf["open_pnl"] == 20.0
    assert perf["open_trades"] == 2


def test_compute_mt5_performance_returns_empty_dict_on_bridge_error(fresh_db, engine):
    engine._bridge = _FakeBridge(raises=True)
    perf = asyncio.run(SimulationEngine.compute_mt5_performance(engine, days=90))
    assert perf == {}


# ── import_mt5_history ─────────────────────────────────────────────────────────

def test_import_mt5_history_no_deals_returns_error(fresh_db, engine):
    engine._bridge = _FakeBridge(deals=[])
    result = asyncio.run(SimulationEngine.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert "error" in result


def test_import_mt5_history_inserts_new_closed_position(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0,
              price=2400.0, volume=0.10, comment=""),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0,
              price=2410.0, profit=100.0, comment="tp hit"),
    ])
    result = asyncio.run(SimulationEngine.import_mt5_history(engine, days=90))
    assert result["imported"] == 1
    assert result["skipped"] == 0

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE mt5_ticket=?", (555,)).fetchone()
        )
    assert row is not None
    assert row["direction"] == "BUY"
    assert row["exit_reason"] == "TP"
    assert row["status"] == "closed"


def test_import_mt5_history_skips_existing_ticket(fresh_db, engine):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-1", "BUY", 2399.0, 2401.0, 2390.0, "closed", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t-1", "sig-1", 555, "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.0, 2390.0,
             "closed", time.time()),
        )
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0, price=2410.0, profit=100.0),
    ])
    result = asyncio.run(SimulationEngine.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_import_mt5_history_skips_position_missing_open_or_close_deal(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=777, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        # no close deal (entry in (1,2,3)) for position 777
    ])
    result = asyncio.run(SimulationEngine.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_import_mt5_history_updates_sim_balance(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0, price=2410.0, profit=100.0),
    ])
    asyncio.run(SimulationEngine.import_mt5_history(engine, days=90))
    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == 1100.0  # seeded 1000.0 + 100.0 imported profit
