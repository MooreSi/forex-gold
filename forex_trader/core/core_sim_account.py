"""Simulation account bookkeeping -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.get_sim_account/update_sim_balance/
reset_simulation, as part of the core/engine.py migration series. See
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.

reset_simulation's 3-statement sequence was already atomic in the original
(one existing `with db_module.db():` block) -- confirmed in 010, preserved
as-is here, nothing to fix.
"""
from __future__ import annotations

import time

from forex_trader.core import database as db_module


def get_sim_account() -> dict:
    with db_module.db() as conn:
        return db_module.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulation_account WHERE id=1").fetchone()
        )


def update_sim_balance(delta: float) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
            (delta,),
        )


def reset_simulation(starting_balance: float) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_simulation_account SET balance=?, reset_at=? WHERE id=1",
            (starting_balance, time.time()),
        )
        conn.execute("DELETE FROM vantage_simulated_trades")
        conn.execute("DELETE FROM vantage_partial_closes")
        conn.execute(
            "UPDATE vantage_signals SET status='cancelled' WHERE status IN ('pending','active')"
        )
