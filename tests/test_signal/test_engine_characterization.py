"""Characterizes the testable surface of TestSignalEngine before task 030
splits it -- see docs/todo/refactor/test-signal-migration/010-*.md.

Scope note (same as the other two packs): async orchestration
(_run_cycle, _velocity_loop, _watchdog_loop, _outcome_loop, _execute_live,
_run_batch_analysis) heavily externally coupled -- left uncovered by
design. Covered: module-level pure functions and _close_signal's balance
math run against a real isolated DB.
"""
import os
import tempfile

import pytest

from forex_trader.test_signal import database as db
from forex_trader.test_signal.engine import (
    TestSignalEngine, _calc_lot_size, _calc_pnl_dollars, _compute_cost_pts,
    _compute_swing_levels,
)


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    os.remove(path)


@pytest.fixture
def engine(fresh_db):
    return TestSignalEngine(bridge=None)


def _sig(**overrides) -> dict:
    base = {
        "direction": "BUY",
        "entry_low": 2399.0, "entry_high": 2401.0, "entry_mid": 2400.0,
        "stop_loss": 2390.0,
    }
    base.update(overrides)
    return base


# ── _calc_lot_size ────────────────────────────────────────────────────────────

def test_calc_lot_size_uses_fixed_lot_when_given():
    lot, risk = _calc_lot_size(balance=1000.0, sl_dist=10.0, fixed_lot=0.10)
    assert lot == 0.10
    assert risk == round(0.10 * 10.0 * 100.0, 2)


def test_calc_lot_size_risk_based_when_no_fixed_lot():
    lot, risk = _calc_lot_size(balance=1000.0, sl_dist=10.0, risk_pct=0.01, fixed_lot=0.0)
    assert risk == 10.0  # 1% of 1000
    assert lot >= 0.01


def test_calc_lot_size_floors_to_min_lot_on_zero_sl_dist():
    lot, risk = _calc_lot_size(balance=1000.0, sl_dist=0.0, fixed_lot=0.0)
    assert lot == 0.01


# ── _calc_pnl_dollars ─────────────────────────────────────────────────────────

def test_calc_pnl_dollars():
    assert _calc_pnl_dollars(pnl_pts=10.0, lot_size=0.10) == 100.0
    assert _calc_pnl_dollars(pnl_pts=-5.0, lot_size=0.10) == -50.0


# ── _compute_cost_pts ─────────────────────────────────────────────────────────

def test_compute_cost_pts_falls_back_when_fee_settings_unavailable():
    cost = _compute_cost_pts(0.30)
    assert cost == 0.35  # fallback: spread + 0.05


# ── _compute_swing_levels ─────────────────────────────────────────────────────

def test_compute_swing_levels_insufficient_candles_returns_zeros():
    assert _compute_swing_levels([]) == (0.0, 0.0)
    assert _compute_swing_levels([{"high": 1, "low": 1}] * 3) == (0.0, 0.0)


def test_compute_swing_levels_returns_high_low_of_last_20():
    candles = [{"high": 2400 + i, "low": 2390 + i} for i in range(25)]
    sh, sl = _compute_swing_levels(candles)
    assert sh == max(c["high"] for c in candles[-20:])
    assert sl == min(c["low"] for c in candles[-20:])


# ── _close_signal -- the money math, run against a real isolated DB ──────────

@pytest.mark.asyncio
async def test_close_signal_full_lifecycle_balance_math(engine, fresh_db):
    sig_id, ref = fresh_db.insert_signal(_sig(entry_mid=2400.0))
    fresh_db.update_signal_triggered(sig_id, 2400.5)

    await engine._close_signal(
        sig_id, outcome="win", close_px=2410.5, pnl_pts=10.0,
        lot_size=0.10, direction="BUY", spread=0.30,
    )

    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    # gross 10.0pts, cost = 0.30 + 0.05 fallback = 0.35, net = 9.65pts * 0.10 * 100 = $96.50
    assert row["pnl_dollars"] == 96.50
    assert fresh_db.get_virtual_balance() == 1096.50
    assert row["balance_after"] == 1096.50


@pytest.mark.asyncio
async def test_close_signal_loss_reduces_balance(engine, fresh_db):
    sig_id, ref = fresh_db.insert_signal(_sig(entry_mid=2400.0))
    fresh_db.update_signal_triggered(sig_id, 2400.0)

    await engine._close_signal(
        sig_id, outcome="loss", close_px=2390.0, pnl_pts=-10.0,
        lot_size=0.10, direction="BUY", spread=0.30,
    )
    row = fresh_db.get_signal_by_id(sig_id)
    # net = -10.0 - 0.35 = -10.35pts * 0.10 * 100 = -$103.50
    assert row["pnl_dollars"] == -103.50
    assert fresh_db.get_virtual_balance() == 896.50
