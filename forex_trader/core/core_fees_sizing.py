"""Fee calculation, P&L, and lot-sizing math -- extracted verbatim (no
logic changes) from core/engine.py's SimulationEngine.calculate_fees/pnl/
suggest_lot_size, as part of the core/engine.py migration series. See
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.

None of these three functions ever used `self` in the original -- they
extract cleanly as plain functions taking explicit parameters, calling
the shared forex_trader.core.database module (db_module) directly. No
parallel repo needed: db_module already provides real transactional
semantics (thread-local, re-entrant db()), unlike the isolated per-engine
databases the reversal_engine/breakout_signal/test_signal packs migrated.
"""
from __future__ import annotations

import math

from forex_trader.core import database as db_module
from forex_trader.core.models import CONTRACT_SIZE, POINT_SIZE


def calculate_fees(lot_size: float, spread: float, hold_hours: float = 0.0) -> dict:
    fs = db_module.get_fee_settings()
    spread_cost = 0.0
    if fs.get("include_spread_cost", 1):
        spread_cost = spread * lot_size * CONTRACT_SIZE

    commission = (
        fs.get("commission_per_lot_per_side", 0.0) * lot_size * 2
        + fs.get("commission_round_turn_per_lot", 0.0) * lot_size
    )
    swap_cost = 0.0
    if fs.get("include_swap_cost", 1) and hold_hours >= 24:
        nights = math.floor(hold_hours / 24)
        swap_cost = fs.get("swap_per_lot_per_night", -6.5) * lot_size * nights

    slippage_pts  = fs.get("estimated_slippage_points", 5.0)
    slippage_cost = slippage_pts * POINT_SIZE * lot_size * CONTRACT_SIZE

    return {
        "spread_cost":   round(spread_cost, 4),
        "commission":    round(commission, 4),
        "swap_cost":     round(swap_cost, 4),
        "slippage_cost": round(slippage_cost, 4),
        "total_cost":    round(spread_cost + commission + abs(swap_cost) + slippage_cost, 4),
    }


def pnl(direction: str, entry: float, current: float, lots: float) -> float:
    m = 1.0 if direction.upper() == "BUY" else -1.0
    return round((current - entry) * m * lots * CONTRACT_SIZE, 4)


def suggest_lot_size(entry: float, stop_loss: float, balance: float, risk_pct: float) -> float:
    distance = abs(entry - stop_loss)
    if distance <= 0:
        return 0.01
    rs         = db_module.get_risk_settings()
    risk_amt   = balance * (risk_pct / 100)
    lot        = risk_amt / (distance * CONTRACT_SIZE)
    min_lot    = 0.01
    max_lot    = float(rs.get("max_lot_size", 0.10))
    return max(min_lot, min(max_lot, round(lot, 2)))
