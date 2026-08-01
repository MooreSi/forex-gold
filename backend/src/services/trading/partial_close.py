"""Partial-close DB accounting -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.partial_close_trade, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-partial-close-migration/020-*.md.

Never calls the MT5 bridge -- the actual broker-side partial close happens
at the (deferred) strategy-handler call site before this runs; this
function only records the DB-side bookkeeping afterward. Calls
core_fees_sizing.pnl() (pack 1) instead of self.pnl().
"""
from __future__ import annotations

import time

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.trading.fees_sizing import pnl as _pnl


async def partial_close_trade(trade_id: str, lots_to_close: float,
                              close_price: float, reason: str = "TP") -> dict:
    row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
    if not row or row["status"] != "open":
        raise ValueError(f"Trade {trade_id} is not open")

    direction   = row["direction"]
    remaining   = float(row["remaining_lots"])
    entry_price = float(row["entry_price"])
    lots_to_close = min(lots_to_close, remaining)
    partial_pnl   = _pnl(direction, entry_price, close_price, lots_to_close)
    new_remaining = round(remaining - lots_to_close, 4)
    now = time.time()

    await db_module.to_db_thread(
        trade_repo.apply_partial_close_with_reason,
        trade_id, now, lots_to_close, close_price, partial_pnl,
        new_remaining, entry_price, row, reason,
    )

    return {
        "trade_id":     trade_id,
        "lots_closed":  lots_to_close,
        "remaining_lots": new_remaining,
        "partial_pnl":  partial_pnl,
        "auto_closed":  new_remaining <= 0,
    }
