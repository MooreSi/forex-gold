"""Profit sync -- extracted from core/engine.py's SimulationEngine
._sync_profit/_schedule_profit_sync/_profit_sweep, then brought up to
upstream's 2026-07-30 grid-leg rewrite by the 2026-08-25 merge.

sync_profit now sums EVERY leg of an EA Template grid, not just the anchor
that promoted the row -- see its docstring for the incident that found it.

Calls bridge.close_position -- a real MT5 order-close call (only on a
detected residual position). This module places no order itself; it only
calls whatever `bridge` its caller supplies.

close_full_after_tps is deliberately NOT here: 2847e32 deleted this module's
copy as an unwired extraction (the live implementation stayed inline and now
lives in backend/src/runtime.py). Do not re-add it from upstream without
resolving that duplication first.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import alerts
from backend.src.services.trading.close_trade import CloseTradeContext, record_close
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)


async def sync_profit(trade_id: str, mt5_ticket: int, bridge: Any) -> Optional[float]:
    """Sync `trade_id`'s real MT5 profit into net_pnl/mt5_profit, correcting
    the simulated balance by the difference from whatever estimate the
    strategy handler had recorded.

    An EA Template trade opens one broker position per Anchor/Grid leg, but
    `mt5_ticket` is only the ONE leg that promoted the row -- summing that
    ticket alone silently dropped every sibling leg's real money from
    net_pnl (confirmed live 2026-07-30: a 3-leg grid's anchor alone showed
    $30.63 while its two grid legs, each its own broker position, were
    never counted anywhere in this figure). Siblings are discovered via the
    EA's own order comment (ea_bridge.find_template_leg_tickets, the same
    link History's leg attribution and Reversal Engine's live reconciliation
    already use independently).

    A sibling can close well after the anchor -- one did, four minutes past
    the row's own close_time, in the same incident -- so this sums whatever
    discovered legs have closed SO FAR rather than waiting for all of them,
    and only marks the trade's mt5_profit as final (stopping
    schedule_profit_sync's retries) once none of the discovered legs are
    still open. Until then it keeps applying the INCREMENTAL correction
    each call, so the total converges as later legs settle instead of
    freezing at whichever leg happened to close first. profit_sweep's
    periodic catch-all (mt5_profit IS NULL, no age limit on that branch)
    is what eventually catches a leg that fills and closes long after the
    anchor -- nothing here watches for that in real time.

    A resting pending leg that has never filled produces no opening deal,
    so it is invisible to find_template_leg_tickets and cannot block
    "settled" -- there is nothing yet to wait for.
    """
    def _fetch_strategy():
        with db_module.db() as conn:
            row = conn.execute(
                "SELECT strategy, direction, initial_sl "
                "FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
            return db_module.row_to_dict(row) if row else {}
    _row = await db_module.to_db_thread(_fetch_strategy)
    strategy   = _row.get("strategy")
    initial_sl = _row.get("initial_sl")

    ticket_set = {int(mt5_ticket)}
    if strategy and strategy.startswith("template:"):
        try:
            from backend.src.services.broker.ea_bridge import find_template_leg_tickets
            ticket_set |= await find_template_leg_tickets(trade_id, bridge)
        except Exception as exc:
            log.debug("[ProfitSync] leg lookup failed for %s: %s", trade_id, exc)

    live_tickets: set[int] = set()
    if len(ticket_set) > 1:
        try:
            live_tickets = {int(p.get("ticket", 0)) for p in (await bridge.get_positions() or [])}
        except Exception:
            pass

    all_deals_cache: Optional[list] = None
    total = 0.0
    any_closed = False
    all_settled = True
    # Exact initial risk, summed the same way net_pnl is: over every leg that
    # actually FILLED. The row's own seed (core_open_trade) had to assume each
    # staged leg would fill at the anchor's price, which a resting pending leg
    # need not do -- it can fill deeper in the zone, closer to the shared stop,
    # or expire without ever taking on risk. Each leg's own opening deal
    # carries the price and volume that resolve it. Skipped entirely when
    # initial_sl is NULL (every row opened before that column existed).
    risk_total = 0.0
    for t in sorted(ticket_set):
        deals = await bridge.get_position_history(t)
        if not deals:
            if all_deals_cache is None:
                all_deals_cache = await bridge.get_deal_history(90)
            deals = [d for d in all_deals_cache if str(d.get("position_id", "")) == str(t)]
        if not deals:
            if t == int(mt5_ticket) or t in live_tickets:
                all_settled = False
            continue
        closing = [d for d in deals if d.get("entry") in (1, 2)]
        if not closing:
            all_settled = False
            continue
        any_closed = True
        total += sum(float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
                     for d in deals)
        if initial_sl:
            for d in deals:
                if d.get("entry") != 0:
                    continue
                risk_total += (abs(float(d.get("price", 0)) - float(initial_sl))
                               * float(d.get("volume", 0)) * CONTRACT_SIZE)

    if not any_closed:
        return None
    mt5_profit = round(total, 2)

    def _apply_profit_sync():
        with db_module.db() as conn:
            existing = db_module.row_to_dict(conn.execute(
                "SELECT mt5_profit, net_pnl FROM vantage_simulated_trades WHERE trade_id=?",
                (trade_id,),
            ).fetchone())
            # mt5_profit stays NULL until all_settled (see below), so this
            # branch runs on every retry while sibling legs are still
            # closing -- our_estimate is always what the LAST call wrote,
            # so the correction applied is exactly the incremental amount
            # a newly-closed leg added, never a re-application of the same
            # money twice.
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
            # Absolute value, not an increment, so re-running it as later
            # legs settle is idempotent -- it just converges on the full
            # basket the same way net_pnl above does. Written on every pass
            # rather than only at all_settled so R:R matches whatever P&L
            # the row is currently showing, over the same set of legs.
            if risk_total > 0:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET initial_risk=? WHERE trade_id=?",
                    (round(risk_total, 4), trade_id),
                )
            if all_settled:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET mt5_profit=? WHERE trade_id=?",
                    (mt5_profit, trade_id),
                )
    await db_module.to_db_thread(_apply_profit_sync)
    return mt5_profit if all_settled else None


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
