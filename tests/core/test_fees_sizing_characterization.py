"""Characterizes calculate_fees/pnl/suggest_lot_size on SimulationEngine
(core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-fees-risk-governor-migration/010-*.md.

None of these three methods use `self` -- called via the class directly
(SimulationEngine.calculate_fees(None, ...)) rather than constructing a
full engine instance, which would need a live bridge/config.
"""
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


def _reset_thread_local_connection():
    """db_module caches a thread-local sqlite3 connection -- db.init(path)
    alone does NOT close/replace it. See test_sim_account_characterization
    .py for the full explanation; must be reset for tests to be isolated."""
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
    # get_risk_settings() has a module-level TTL cache shared across the
    # whole process -- invalidate it so a previous test's temp DB values
    # can't leak into this one.
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def test_calculate_fees_default_settings(fresh_db):
    fees = SimulationEngine.calculate_fees(None, lot_size=0.10, spread=0.30, hold_hours=0.0)
    # defaults: include_spread_cost=1, include_swap_cost=1, slippage=5.0pts,
    # commission=0, no swap (hold_hours < 24)
    assert fees["spread_cost"] == round(0.30 * 0.10 * 100, 4)  # CONTRACT_SIZE=100 for XAUUSD
    assert fees["commission"] == 0.0
    assert fees["swap_cost"] == 0.0
    assert fees["slippage_cost"] > 0
    assert fees["total_cost"] == round(
        fees["spread_cost"] + fees["commission"] + abs(fees["swap_cost"]) + fees["slippage_cost"], 4
    )


def test_calculate_fees_swap_applies_after_24h(fresh_db):
    fees_short = SimulationEngine.calculate_fees(None, lot_size=0.10, spread=0.30, hold_hours=12.0)
    fees_long  = SimulationEngine.calculate_fees(None, lot_size=0.10, spread=0.30, hold_hours=48.0)
    assert fees_short["swap_cost"] == 0.0
    assert fees_long["swap_cost"] != 0.0  # 2 nights of swap


def test_calculate_fees_spread_cost_toggle(fresh_db):
    db.update_fee_settings({"include_spread_cost": 0})
    fees = SimulationEngine.calculate_fees(None, lot_size=0.10, spread=0.30, hold_hours=0.0)
    assert fees["spread_cost"] == 0.0


def test_calculate_fees_commission(fresh_db):
    db.update_fee_settings({
        "commission_per_lot_per_side": 3.5,
        "commission_round_turn_per_lot": 0.0,
    })
    fees = SimulationEngine.calculate_fees(None, lot_size=0.10, spread=0.30, hold_hours=0.0)
    assert fees["commission"] == round(3.5 * 0.10 * 2, 4)


def test_pnl_buy_direction(fresh_db):
    result = SimulationEngine.pnl("BUY", entry=2400.0, current=2410.0, lots=0.10)
    assert result == round(10.0 * 0.10 * 100, 4)


def test_pnl_sell_direction(fresh_db):
    result = SimulationEngine.pnl("SELL", entry=2400.0, current=2390.0, lots=0.10)
    assert result == round(10.0 * 0.10 * 100, 4)  # SELL profits when price drops


def test_pnl_negative_for_adverse_move(fresh_db):
    result = SimulationEngine.pnl("BUY", entry=2400.0, current=2390.0, lots=0.10)
    assert result < 0


def test_suggest_lot_size_risk_based(fresh_db):
    lot = SimulationEngine.suggest_lot_size(
        None, entry=2400.0, stop_loss=2390.0, balance=1000.0, risk_pct=1.0,
    )
    # risk_amt = 1000 * 0.01 = 10; distance = 10; lot = 10 / (10 * 100) = 0.01
    assert lot == 0.01


def test_suggest_lot_size_clamped_to_max_lot_size(fresh_db):
    db.update_risk_settings({"max_lot_size": 0.05})
    lot = SimulationEngine.suggest_lot_size(
        None, entry=2400.0, stop_loss=2395.0, balance=100000.0, risk_pct=5.0,
    )
    assert lot == 0.05


def test_suggest_lot_size_zero_distance_returns_minimum(fresh_db):
    lot = SimulationEngine.suggest_lot_size(
        None, entry=2400.0, stop_loss=2400.0, balance=1000.0, risk_pct=1.0,
    )
    assert lot == 0.01
