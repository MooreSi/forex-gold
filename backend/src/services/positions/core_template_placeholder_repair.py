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
import time
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.positions import repair_repo
from backend.src.services.trading import trade_repo
from backend.src.services.telegram import alerts as telegram_alerts

log = logging.getLogger(__name__)

# Matches the EA's own comment construction: "ea:" + StringSubstr(trade_id, 0, 10)
def _comment_prefix(trade_id: str) -> str:
    # Single source of truth for the EA's comment format (ea_bridge), so the
    # id length cannot drift between this module, the history channel lookup
    # and the Reversal Engine's leg reconciliation, which all depend on it.
    from backend.src.services.broker.ea_bridge import comment_for_trade
    return comment_for_trade(trade_id)


# _fetch_placeholders / _fetch_row are gone: the first is
# repair_repo.fetch_template_placeholders, the second was a byte-identical
# duplicate of trade_repo.get_trade.


def placeholder_no_fill_expiry_secs() -> int:
    """How long a placeholder with no broker evidence at all is kept before it
    is written off as never filled. Settings > Expert Tunables."""
    from backend.src.services.risk import expert_params
    return expert_params.get("placeholder_no_fill_expiry_s")


async def repair_template_placeholders(bridge: Any) -> int:
    """Adopt or close every open $0-entry placeholder row whose broker leg can
    be identified. Returns how many rows were repaired. Never raises."""
    repaired = 0
    try:
        if not bridge.is_configured():
            return 0
        rows = await db_module.to_db_thread(repair_repo.fetch_template_placeholders)
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
                # No live leg and no opening deal: the broker has never heard
                # of this trade. Below the expiry that is not conclusive --
                # its legs may still be resting as pending orders -- so it is
                # left alone, exactly as before.
                #
                # Past the expiry nothing is coming, and leaving it open is
                # not free: open_trade()'s max-open-trades gate counts open
                # rows without asking the broker, so a dead row permanently
                # consumes one of the user's slots (bugs/016 -- 26 hours and
                # one slot in five, on the live demo account).
                #
                # Neither event-driven path can rescue this row. The fill
                # never arrived, so _promote_leg_fill never ran; and the EA's
                # open ack never arrived either, leaving grid_legs_total NULL,
                # which _on_grid_leg_cancelled's own expiry explicitly
                # declines to act on ("unknown, don't touch"). Polling by age
                # is the only thing left that can tell "never existed" from
                # "still resting".
                if await _expire_never_filled(row, bridge):
                    repaired += 1
                continue
            if await _close_from_deals(row, open_deal, deals, bridge):
                repaired += 1
        except Exception as e:
            log.warning("[TemplateRepair] %s failed: %s", trade_id[:8], e)
    return repaired


async def _expire_never_filled(row: dict, bridge: Any) -> bool:
    """Write off a placeholder the broker has no record of, once it is old
    enough that nothing can still be coming.

    Uses record_close() with a 0.0 price and the SAME "no_fill_expired" reason
    the event-driven path already uses for a grid whose every leg cancelled
    unfilled (ea_bridge._events._close_dead_grid_placeholder) -- this is that
    close reached by polling instead of by event, not a new kind of close.

    record_close() makes no broker call and its entry_price==0 guard records
    P&L from mt5_profit rather than computing one from a zero entry, so this
    cannot fabricate a figure and cannot touch a real position. There is no
    real position: that is the precondition for getting here.
    """
    from backend.src.services.trading.close_trade import CloseTradeContext, record_close

    trade_id = row["trade_id"]
    age_s = time.time() - float(row.get("open_time") or 0)
    if age_s < placeholder_no_fill_expiry_secs():
        return False

    try:
        ctx = CloseTradeContext(bridge)
        await record_close(trade_id, 0.0, "no_fill_expired", ctx)
    except Exception as e:
        log.warning("[TemplateRepair] failed to expire never-filled placeholder "
                    "trade=%s: %s", trade_id[:8], e)
        return False

    log.warning(
        "[TemplateRepair] expired never-filled placeholder trade=%s after %.1fh "
        "— no broker position and no broker deal in 7 days of history, so no "
        "leg was ever filled. Closed at $0 P&L; it was holding a trade slot.",
        trade_id[:8], age_s / 3600.0,
    )
    asyncio.create_task(telegram_alerts.send_message(
        f"EA Template placeholder written off — {row['direction']} "
        f"{row.get('tg_source', '')} never filled and the broker has no record "
        f"of it after {age_s / 3600.0:.0f}h. No position was ever opened and "
        f"there is no P&L. It was holding one of your open-trade slots.",
        trade_id, "template_placeholder_no_fill_expired",
    ))
    return True


async def _adopt_live_position(row: dict, pos: dict) -> bool:
    """Promote a placeholder row onto a leg that is still open at the broker."""
    trade_id = row["trade_id"]
    ticket   = int(pos.get("ticket", 0) or 0)
    entry    = float(pos.get("open_price", 0) or 0)
    lots     = round(float(pos.get("volume", 0) or 0), 4)
    if not ticket or entry <= 0 or lots <= 0:
        return False

    await db_module.to_db_thread(
        repair_repo.adopt_placeholder_onto_leg, trade_id, ticket, entry, lots)
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

    # The real fill is written FIRST so record_close() below computes against
    # a genuine entry price rather than fabricating one from a zero entry.
    await db_module.to_db_thread(
        repair_repo.record_placeholder_fill, trade_id,
        ticket or pos_id, entry,
        lots or row["lot_size"], lots or row["remaining_lots"], profit)

    ctx = CloseTradeContext(bridge)
    result = await record_close(trade_id, close_px, reason, ctx)
    log.warning(
        "[TemplateRepair] closed orphaned placeholder trade=%s from broker deal "
        "history: position=%s entry %.2f -> exit %.2f, realised $%.2f (%s)",
        trade_id[:8], pos_id, entry, close_px, profit, reason,
    )
    closed_row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
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



