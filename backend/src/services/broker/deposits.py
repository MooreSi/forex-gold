"""Net deposits calculation -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.get_total_deposits, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-mt5-history-migration/020-*.md.

Takes `bridge` as an explicit parameter (anything exposing async
get_deal_history(days: int) -> list[dict], matching MT5BridgeClient's real
shape) instead of reading self._bridge.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from backend.src.db import database as db_module

log = logging.getLogger(__name__)


async def get_total_deposits(bridge: Any) -> float:
    """Net funding since account inception (deposits minus withdrawals) — used
    to compute lifetime P&L as equity minus deposits, independent of any
    rolling trade-history window. Cached for an hour since this only
    changes when money actually moves in/out of the account, unlike trade
    P&L which changes constantly; recomputing it on every UI refresh tick
    would mean an extra full deal-history fetch for a number that's almost
    always unchanged."""
    cached = db_module.get_app_config("mt5_total_deposits_cache")
    if cached:
        try:
            data = json.loads(cached)
            if time.time() - data.get("computed_at", 0) < 3600:
                return float(data["total"])
        except Exception:
            pass
    try:
        deals = await bridge.get_deal_history(3650) or []  # effectively whole account life
        total = round(sum(
            float(d.get("profit", 0)) for d in deals if not d.get("position_id")
        ), 2)
        db_module.set_app_config(
            "mt5_total_deposits_cache",
            json.dumps({"total": total, "computed_at": time.time()}),
        )
        return total
    except Exception as e:
        log.debug("get_total_deposits error: %s", e)
        return 0.0
