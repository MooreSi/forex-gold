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

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.core_close_trade import CloseTradeContext, record_close

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
    def _apply_profit_sync():
        with db_module.db() as conn:
            existing = db_module.row_to_dict(conn.execute(
                "SELECT mt5_profit, net_pnl FROM vantage_simulated_trades WHERE trade_id=?",
                (trade_id,),
            ).fetchone())
            # First-time sync only: if our estimated net_pnl differs from the real MT5
            # figure (e.g. due to SL slippage, swap, commission), correct the simulated
            # account balance so it stays in sync with the broker.
            if existing and existing.get("mt5_profit") is None:
                our_estimate = float(existing.get("net_pnl") or 0)
                correction = round(mt5_profit - our_estimate, 4)
                if abs(correction) >= 0.01:
                    conn.execute(
                        "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
                        (correction,),
                    )
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET net_pnl=? WHERE trade_id=?",
                        (mt5_profit, trade_id),
                    )
                    log.info(
                        "[ProfitSync] Balance corrected for %s: estimated=%.2f mt5=%.2f adj=%.2f",
                        trade_id, our_estimate, mt5_profit, correction,
                    )
            conn.execute(
                "UPDATE vantage_simulated_trades SET mt5_profit=? WHERE trade_id=?",
                (mt5_profit, trade_id),
            )
    await db_module.to_db_thread(_apply_profit_sync)
    return mt5_profit


async def schedule_profit_sync(trade_id: str, mt5_ticket: int, bridge: Any) -> None:
    def _fetch_profit():
        with db_module.db() as conn:
            return conn.execute(
                "SELECT mt5_profit FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
    for delay in (0, 10, 60, 300, 1800):
        if delay:
            await asyncio.sleep(delay)
        row = await db_module.to_db_thread(_fetch_profit)
        if row and row[0] is not None:
            return
        result = await sync_profit(trade_id, mt5_ticket, bridge)
        if result is not None:
            return


async def profit_sweep(bridge: Any) -> None:
    cutoff = time.time() - 86400
    def _fetch_unsynced():
        with db_module.db() as conn:
            return conn.execute(
                """SELECT trade_id,mt5_ticket FROM vantage_simulated_trades
                   WHERE status='closed' AND mt5_ticket IS NOT NULL
                     AND (mt5_profit IS NULL OR (mt5_profit=0.0 AND close_time>?))""",
                (cutoff,),
            ).fetchall()
    rows = await db_module.to_db_thread(_fetch_unsynced)
    for row in rows:
        try:
            await sync_profit(row[0], int(row[1]), bridge)
        except Exception as e:
            log.debug("profit sweep error %s: %s", row[0], e)


