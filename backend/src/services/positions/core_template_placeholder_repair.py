"""Self-heal for EA Template placeholder rows that never got promoted.

An EA Template trade is written to vantage_simulated_trades as a deliberate
placeholder (mt5_ticket=0, entry_price=0) at open time: the EA opens each
Anchor/Grid leg as its own broker position and reports it back under a
suffixed trade_id ("<trade_id>-a<N>" / "-g<N>"), and
ea_bridge._promote_leg_fill turns the first leg to go live into the row's
real ticket/entry.

That promotion is event-driven, so anything that stops the event arriving --
Python not running at fill time, a dropped socket, an EA restart -- leaves
the row a permanent $0-entry ghost in Active Trades: no ticket to close, no
entry to measure against, and (before the guards in core_monitor_loop.
check_sl and core_close_trade.record_close) a fabricated P&L the moment
anything did try to close it.

This module closes that hole from the other direction, by polling instead of
by event:

  * a placeholder whose leg is still open at the broker is adopted -- the
    row takes that position's ticket, entry price and volume, exactly as
    _promote_leg_fill would have;
  * a placeholder whose leg has already opened AND closed is recorded closed
    with the broker's own close price and realised profit;
  * a placeholder with no matching broker deal at all is left alone (its
    legs may still be resting as pending orders).

Legs are matched by the order comment the EA stamps on every leg,
"ea:<first 10 chars of trade_id><a|g><N>" (HandleOpenTemplateGrid in
ForexTraderBridge.mq5) -- the only link back to the app's trade_id that
survives into MT5's own position/deal records.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts

log = logging.getLogger(__name__)

# Matches the EA's own comment construction: "ea:" + StringSubstr(trade_id, 0, 10)
def _comment_prefix(trade_id: str) -> str:
    # Single source of truth for the EA's comment format (ea_bridge), so the
    # id length cannot drift between this module, the history channel lookup
    # and the Reversal Engine's leg reconciliation, which all depend on it.
    from backend.src.services.broker.ea_bridge import comment_for_trade
    return comment_for_trade(trade_id)


def _fetch_placeholders() -> list:
    with db_module.db() as conn:
        return [db_module.row_to_dict(r) for r in conn.execute(
            # entry_price=0 is the defect signature: a row with no ticket but a
            # real entry price is a legitimately ticket-less simulated trade,
            # not an unpromoted EA Template placeholder.
            "SELECT * FROM vantage_simulated_trades "
            "WHERE status='open' AND (mt5_ticket IS NULL OR mt5_ticket=0) "
            "AND (entry_price IS NULL OR entry_price=0)"
        ).fetchall()]


async def repair_template_placeholders(bridge: Any) -> int:
    """Adopt or close every open $0-entry placeholder row whose broker leg can
    be identified. Returns how many rows were repaired. Never raises."""
    repaired = 0
    try:
        if not bridge.is_configured():
            return 0
        rows = await db_module.to_db_thread(_fetch_placeholders)
        if not rows:
            return 0
        positions = await bridge.get_positions()
        if positions is None:
            return 0
        deals = await bridge.get_deal_history(7) or []
    except Exception as e:
        log.debug("[TemplateRepair] pre-checks failed: %s", e)
        return 0

    for row in rows:
        trade_id = row["trade_id"]
        prefix   = _comment_prefix(trade_id)
        try:
            live = next(
                (p for p in positions
                 if str(p.get("comment") or "").startswith(prefix)), None,
            )
            if live is not None:
                if await _adopt_live_position(row, live):
                    repaired += 1
                continue

            # No live leg -- did one ever open? The opening deal carries the
            # comment; its position_id links to the closing deal, which is
            # where the broker's real close price and realised profit live.
            open_deal = next(
                (d for d in deals
                 if str(d.get("comment") or "").startswith(prefix)
                 and int(d.get("entry", 0) or 0) == 0), None,
            )
            if open_deal is None:
                continue  # legs may still be resting as pending orders
            if await _close_from_deals(row, open_deal, deals, bridge):
                repaired += 1
        except Exception as e:
            log.warning("[TemplateRepair] %s failed: %s", trade_id[:8], e)
    return repaired


async def _adopt_live_position(row: dict, pos: dict) -> bool:
    """Promote a placeholder row onto a leg that is still open at the broker."""
    trade_id = row["trade_id"]
    ticket   = int(pos.get("ticket", 0) or 0)
    entry    = float(pos.get("open_price", 0) or 0)
    lots     = round(float(pos.get("volume", 0) or 0), 4)
    if not ticket or entry <= 0 or lots <= 0:
        return False

    def _apply():
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,"
                "entry_low=?,entry_high=?,lot_size=?,remaining_lots=? "
                "WHERE trade_id=? AND status='open' AND (mt5_ticket IS NULL OR mt5_ticket=0)",
                (ticket, entry, entry, entry, lots, lots, trade_id),
            )
    await db_module.to_db_thread(_apply)
    log.warning(
        "[TemplateRepair] adopted orphaned placeholder trade=%s onto live leg "
        "ticket=%s @ %.2f %.2f lots (comment=%r) — its fill event never reached "
        "this node",
        trade_id[:8], ticket, entry, lots, pos.get("comment"),
    )
    return True


async def _close_from_deals(row: dict, open_deal: dict, deals: list,
                            bridge: Any) -> bool:
    """Record a placeholder closed using the broker's own deal record for the
    leg that opened and closed while this node wasn't listening."""
    from backend.src.services.telegram import alerts
    from backend.src.services.trading.close_trade import CloseTradeContext, record_close

    trade_id = row["trade_id"]
    pos_id   = int(open_deal.get("position_id", 0) or 0)
    ticket   = int(open_deal.get("order") or open_deal.get("ticket") or 0)
    entry    = float(open_deal.get("price", 0) or 0)
    lots     = round(float(open_deal.get("volume", 0) or 0), 4)
    close_deals = [
        d for d in deals
        if int(d.get("position_id", 0) or 0) == pos_id
        and int(d.get("entry", 0) or 0) in (1, 2, 3)
    ]
    if not pos_id or not close_deals or entry <= 0:
        # The leg is open per the deal history but absent from get_positions()
        # -- a transient read, not a close. Leave it for the next pass.
        return False
    last     = max(close_deals, key=lambda d: d.get("time", 0))
    close_px = float(last.get("price", 0) or 0)
    profit   = round(sum(
        float(d.get("profit", 0) or 0) + float(d.get("swap", 0) or 0)
        + float(d.get("fee", 0) or 0) for d in close_deals
    ), 2)
    comment  = str(last.get("comment") or "").lower()
    reason   = "SL" if ("sl" in comment or "stop" in comment) else (
        "MT5_sync_TP" if ("tp" in comment or "take" in comment) else "MT5_close"
    )

    def _apply():
        with db_module.db() as conn:
            # Write the real fill onto the row FIRST so record_close() below
            # computes against a genuine entry price (and so mt5_profit is the
            # authoritative figure for the P&L and the Telegram message).
            conn.execute(
                "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,"
                "entry_low=?,entry_high=?,lot_size=?,remaining_lots=?,mt5_profit=? "
                "WHERE trade_id=?",
                (ticket or pos_id, entry, entry, entry,
                 lots or row["lot_size"], lots or row["remaining_lots"],
                 profit, trade_id),
            )
    await db_module.to_db_thread(_apply)

    ctx = CloseTradeContext(bridge)
    result = await record_close(trade_id, close_px, reason, ctx)
    log.warning(
        "[TemplateRepair] closed orphaned placeholder trade=%s from broker deal "
        "history: position=%s entry %.2f -> exit %.2f, realised $%.2f (%s)",
        trade_id[:8], pos_id, entry, close_px, profit, reason,
    )
    closed_row = await db_module.to_db_thread(_fetch_row, trade_id)
    account = None
    try:
        account = await bridge.get_account()
    except Exception:
        pass
    asyncio.create_task(telegram_alerts.send_message(
        telegram_alerts.fmt_trade_close(closed_row, result, {}, account),
        trade_id, f"template_placeholder_repair_{reason.lower()}",
    ))
    return True


def _fetch_row(trade_id: str) -> Optional[dict]:
    with db_module.db() as conn:
        return db_module.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,),
        ).fetchone())
