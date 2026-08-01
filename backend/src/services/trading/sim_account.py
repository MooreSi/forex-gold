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

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo


def get_sim_account() -> dict:
    return trade_repo.get_simulation_account()


def update_sim_balance(delta: float) -> None:
    trade_repo.add_to_sim_balance(delta)


def reset_simulation(starting_balance: float) -> None:
    trade_repo.reset_simulation_data(starting_balance, time.time())
