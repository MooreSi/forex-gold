"""MT5 deal-history backfill import -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.import_mt5_history, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-mt5-history-migration/020-*.md.

Takes `bridge` as an explicit parameter instead of self._bridge, and calls
core_fees_sizing.pnl() (pack 1) instead of self.pnl(). Writes local
vantage_signals/vantage_simulated_trades rows reconstructed from real MT5
deal history for any closed position missing from the DB -- never sends
anything to MT5 itself (backfill only).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from backend.src.db import database as db_module
from backend.src.services.trading.fees_sizing import pnl as _pnl

log = logging.getLogger(__name__)


async def import_mt5_history(bridge: Any, days: int = 90) -> dict:
    """Pull closed deals from MT5 bridge, reconstruct positions, insert any missing into DB."""
    deals = await bridge.get_deal_history(days)
    if not deals:
        return {"imported": 0, "skipped": 0, "error": "No deals returned from bridge"}

    # Group deals by position_id
    by_pos: dict[int, list] = {}
    for d in deals:
        pid = d.get("position_id")
        if pid:  # excludes None and 0 (balance/deposit ops)
            by_pos.setdefault(int(pid), []).append(d)

    with db_module.db() as conn:
        existing_tickets = {
            row[0] for row in conn.execute(
                "SELECT mt5_ticket FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
            ).fetchall()
        }

    imported = skipped = 0
    for ticket, pos_deals in by_pos.items():
        if ticket in existing_tickets:
            skipped += 1
            continue
        # Find open and close deals (use last close deal for multi-partial positions)
        open_deal  = next((d for d in pos_deals if d.get("entry") == 0), None)
        _cd        = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
        close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
        if not open_deal or not close_deal:
            skipped += 1
            continue

        direction   = "BUY" if open_deal.get("type", 0) == 0 else "SELL"
        entry_price = float(open_deal.get("price", 0))
        close_price = float(close_deal.get("price", 0))
        lot_size    = float(open_deal.get("volume", 0.01))
        open_ts     = float(open_deal.get("time", time.time()))
        close_ts    = float(close_deal.get("time", time.time()))
        mt5_profit  = round(
            sum(float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
                for d in pos_deals), 2,
        )
        gross_pnl   = _pnl(direction, entry_price, close_price, lot_size)
        comment     = (close_deal.get("comment") or "").lower()
        if "sl" in comment or "stop" in comment:
            exit_reason = "SL"
        elif "tp" in comment or "take" in comment:
            exit_reason = "TP"
        else:
            exit_reason = "MT5_import"

        signal_id = str(uuid.uuid4())[:16]
        trade_id  = str(uuid.uuid4())[:16]
        with db_module.db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO vantage_signals
                   (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                    notes,status,created_at,activated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, "MT5 Import", direction, entry_price, entry_price, 0.0,
                 f"Imported from MT5 ticket {ticket}", "closed", open_ts, open_ts),
            )
            conn.execute(
                """INSERT OR IGNORE INTO vantage_simulated_trades
                   (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                    lot_size,remaining_lots,stop_loss,status,open_time,close_time,
                    close_price,exit_reason,gross_pnl,realised_pnl,net_pnl,mt5_profit,strategy)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trade_id, signal_id, ticket, direction, entry_price, entry_price, entry_price,
                 lot_size, 0.0, 0.0, "closed", open_ts, close_ts,
                 close_price, exit_reason, gross_pnl, gross_pnl, mt5_profit, mt5_profit,
                 "MT5_import"),
            )
            conn.execute(
                "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
                (mt5_profit,),
            )
        imported += 1
        log.info("MT5 import: ticket=%s %s @ %.2f -> %.2f profit=%.2f",
                 ticket, direction, entry_price, close_price, mt5_profit)

    return {"imported": imported, "skipped": skipped}
