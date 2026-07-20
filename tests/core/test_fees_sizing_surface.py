"""Proves forex_trader.core.core_fees_sizing's extracted functions behave
identically to the SimulationEngine methods characterized in
test_fees_sizing_characterization.py -- see
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_fees_sizing as fs


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


def test_calculate_fees_default_settings(fresh_db):
    fees = fs.calculate_fees(lot_size=0.10, spread=0.30, hold_hours=0.0)
    assert fees["spread_cost"] == round(0.30 * 0.10 * 100, 4)
    assert fees["commission"] == 0.0
    assert fees["swap_cost"] == 0.0
    assert fees["slippage_cost"] > 0
    assert fees["total_cost"] == round(
        fees["spread_cost"] + fees["commission"] + abs(fees["swap_cost"]) + fees["slippage_cost"], 4
    )


def test_calculate_fees_swap_applies_after_24h(fresh_db):
    fees_short = fs.calculate_fees(lot_size=0.10, spread=0.30, hold_hours=12.0)
    fees_long  = fs.calculate_fees(lot_size=0.10, spread=0.30, hold_hours=48.0)
    assert fees_short["swap_cost"] == 0.0
    assert fees_long["swap_cost"] != 0.0


def test_calculate_fees_spread_cost_toggle(fresh_db):
    db.update_fee_settings({"include_spread_cost": 0})
    fees = fs.calculate_fees(lot_size=0.10, spread=0.30, hold_hours=0.0)
    assert fees["spread_cost"] == 0.0


def test_calculate_fees_commission(fresh_db):
    db.update_fee_settings({
        "commission_per_lot_per_side": 3.5,
        "commission_round_turn_per_lot": 0.0,
    })
    fees = fs.calculate_fees(lot_size=0.10, spread=0.30, hold_hours=0.0)
    assert fees["commission"] == round(3.5 * 0.10 * 2, 4)


def test_pnl_buy_direction(fresh_db):
    result = fs.pnl("BUY", entry=2400.0, current=2410.0, lots=0.10)
    assert result == round(10.0 * 0.10 * 100, 4)


def test_pnl_sell_direction(fresh_db):
    result = fs.pnl("SELL", entry=2400.0, current=2390.0, lots=0.10)
    assert result == round(10.0 * 0.10 * 100, 4)


def test_pnl_negative_for_adverse_move(fresh_db):
    result = fs.pnl("BUY", entry=2400.0, current=2390.0, lots=0.10)
    assert result < 0


def test_suggest_lot_size_risk_based(fresh_db):
    lot = fs.suggest_lot_size(entry=2400.0, stop_loss=2390.0, balance=1000.0, risk_pct=1.0)
    assert lot == 0.01


def test_suggest_lot_size_clamped_to_max_lot_size(fresh_db):
    db.update_risk_settings({"max_lot_size": 0.05})
    lot = fs.suggest_lot_size(entry=2400.0, stop_loss=2395.0, balance=100000.0, risk_pct=5.0)
    assert lot == 0.05


def test_suggest_lot_size_zero_distance_returns_minimum(fresh_db):
    lot = fs.suggest_lot_size(entry=2400.0, stop_loss=2400.0, balance=1000.0, risk_pct=1.0)
    assert lot == 0.01
