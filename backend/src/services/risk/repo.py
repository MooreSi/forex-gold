"""Risk service's remaining SQL, collected out of strategy_params.py,
governor.py and schedule.py (M1 SQL sweep). Verbatim statements; the callers
run them from the same places in the same order they always did.
"""
from __future__ import annotations

import logging

from backend.src.db.database import db

log = logging.getLogger(__name__)


# ── strategy_param_templates ──────────────────────────────────────────────────

def list_param_templates(strategy: str) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT id, strategy, name, params_json, created_at FROM strategy_param_templates "
            "WHERE strategy=? ORDER BY created_at DESC", (strategy,),
        ).fetchall()


def insert_param_template(strategy: str, name: str, params_json: str,
                          created_at: float) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO strategy_param_templates (strategy, name, params_json, created_at) "
            "VALUES (?,?,?,?)",
            (strategy, name, params_json, created_at),
        )
        return cur.lastrowid


def delete_param_template(template_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM strategy_param_templates WHERE id=?", (template_id,))


def get_param_template(template_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT strategy, params_json FROM strategy_param_templates WHERE id=?",
            (template_id,),
        ).fetchone()


# ── vantage_simulated_trades: risk-gate aggregate reads ───────────────────────

def count_unprotected_same_direction(direction: str) -> int:
    """Open trades in this direction whose SL has not reached breakeven."""
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades "
            "WHERE status='open' AND direction=? AND sl_moved_to_be=0",
            (direction,),
        ).fetchone()[0]


def sum_realised_pnl_since(day_start: float) -> float:
    with db() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM vantage_simulated_trades "
            "WHERE close_time >= ?", (day_start,),
        ).fetchone()[0] or 0.0


def sum_closed_pnl_opened_between(window_start: float, window_end: float) -> float:
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM vantage_simulated_trades "
            "WHERE status='closed' AND open_time >= ? AND open_time < ?",
            (window_start, window_end),
        ).fetchone()
    return float(row[0] or 0.0)
