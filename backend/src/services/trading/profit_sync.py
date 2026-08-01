"""Profit sync + close-full-after-TPs -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._sync_profit/
_schedule_profit_sync/_profit_sweep/_close_full_after_tps, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-profit-sync-migration/020-*.md.

Calls bridge.close_position -- a real MT5 order-close call (only on a
detected residual position), unchanged from the original. This module
places no order itself; it only calls whatever `bridge` its caller
supplies.

close_full_after_tps is the shared dependency injected as an optional
callable across nearly every TP/SL strategy handler pack already
extracted -- this is its real implementation. Reuses
core_close_trade.CloseTradeContext/record_close (already extracted).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.close_trade import CloseTradeContext, record_close

log = logging.getLogger(__name__)


async def sync_profit(trade_id: str, mt5_ticket: int, bridge: Any) -> Optional[float]:
    deals = await bridge.get_position_history(mt5_ticket)
    if not deals:
        all_deals = await bridge.get_deal_history(90)
        deals = [d for d in all_deals if str(d.get("position_id", "")) == str(mt5_ticket)]
    if not deals:
        return None
    closing = [d for d in deals if d.get("entry") in (1, 2)]
    if not closing:
        return None
    mt5_profit = round(
        sum(float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
            for d in deals), 2,
    )
    await db_module.to_db_thread(trade_repo.apply_profit_sync, trade_id, mt5_profit)
    return mt5_profit


async def schedule_profit_sync(trade_id: str, mt5_ticket: int, bridge: Any) -> None:
    for delay in (0, 10, 60, 300, 1800):
        if delay:
            await asyncio.sleep(delay)
        row = await db_module.to_db_thread(trade_repo.fetch_mt5_profit, trade_id)
        if row and row[0] is not None:
            return
        result = await sync_profit(trade_id, mt5_ticket, bridge)
        if result is not None:
            return


async def profit_sweep(bridge: Any) -> None:
    cutoff = time.time() - 86400
    rows = await db_module.to_db_thread(trade_repo.fetch_unsynced_closed, cutoff)
    for row in rows:
        try:
            await sync_profit(row[0], int(row[1]), bridge)
        except Exception as e:
            log.debug("profit sweep error %s: %s", row[0], e)


