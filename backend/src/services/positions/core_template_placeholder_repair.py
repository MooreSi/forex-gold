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


def placeholder_single_no_fill_expiry_secs() -> int:
    """The same, for a single-mode template (owner, 2026-09-28): one market
    order and no resting legs, so minutes rather than a day. Settings >
    Expert Tunables."""
    from backend.src.services.risk import expert_params
    return expert_params.get("placeholder_single_no_fill_expiry_s")


def _expiry_for(row: dict) -> int:
    """Which expiry applies to this placeholder.

    The short one only when this node can see the template AND it is single
    mode. `pendings` is not consulted: the EA reads it only in
    HandleOpenTemplateGrid, which runs only for tpl_mode "grid", and it
    defaults to 1 on every new template -- requiring 0 kept default-made
    single templates on the 24h expiry (found live on the VPS, 2026-09-28).
    A grid, a template that cannot be read, or a row
    that is not a template at all keeps the long expiry: a resting leg that
    was about to fill must never be written off because its mode was unknown.
    """
    try:
        from backend.src.services.broker import ea_templates
        strategy = row.get("strategy") or ""
        if ea_templates.is_template_override(strategy):
            tpl = ea_templates.get_ea_template(
                ea_templates.template_name_from_override(strategy))
            if tpl is not None and tpl.get("mode") == "single":
                return placeholder_single_no_fill_expiry_secs()
    except Exception as e:
        log.debug("[TemplateRepair] template mode unreadable for %s: %s",
                  str(row.get("trade_id"))[:8], e)
    return placeholder_no_fill_expiry_secs()


async def _orders_held(bridge: Any) -> Optional[list]:
    """The orders MT5 still holds, or None when that cannot be read.

    Bug 070 (2026-09-29): a market order MT5 has sent and the broker has not
    answered ("started", for minutes while Vantage-Demo timed requests out) is
    neither a position nor a deal. Without this read a placeholder waiting for
    it looked exactly like one that never existed, and was written off while
    its order could still fill -- a fill with no row, managed by nothing.

    A bridge with no get_orders at all is one with no order concept (the test
    doubles written before this existed): it answers [] so their behaviour is
    unchanged. Every real bridge has it, and its None is kept as None.
    """
    reader = getattr(bridge, "get_orders", None)
    if reader is None:
        return []
    try:
        return await reader()
    except Exception:
        return None


def _holds_order(orders: list, prefix: str) -> bool:
    return any(str(o.get("comment") or "").startswith(prefix) for o in orders)


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
        positions = await bridge.get_positions() or []
        if positions is None:
            return 0
        deals = await bridge.get_deal_history(7) or []
        orders = await _orders_held(bridge)
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
                #
                # Unless MT5 still holds an order for it, or cannot say (bug
                # 070): then something may still be coming, whatever its age.
                if orders is None or _holds_order(orders, prefix):
                    log.debug("[TemplateRepair] %s kept: %s", trade_id[:8],
                              "order list unreadable" if orders is None
                              else "MT5 still holds its order")
                    continue
                if await _expire_never_filled(row, bridge):
                    repaired += 1
                continue
            if await _close_from_deals(row, open_deal, deals, bridge):
                repaired += 1
        except Exception as e:
            log.warning("[TemplateRepair] %s failed: %s", trade_id[:8], e)
    return repaired


async def write_off_unconfirmed(bridge: Any) -> dict:
    """The owner's "write these off now" (2026-09-28), for placeholders the
    broker has never heard of.

    The automatic pass above waits 24h unless it can read the template as
    single mode; a template renamed, deleted or never synced to this node
    gets the 24h, and its rows hold trade slots all day. This skips the MODE
    question and keeps every EVIDENCE check: a live leg is adopted, an
    opening deal is left for the repair pass, a row younger than the
    single-mode expiry is left alone, and an unreadable broker writes off
    nothing. Same close as the automatic one.

    Returns {"written_off": [trade_id], "kept": [{"trade_id", "reason"}],
    "error": str|None}. Never raises.
    """
    result: dict = {"written_off": [], "kept": [], "error": None}
    try:
        rows = await db_module.to_db_thread(repair_repo.fetch_template_placeholders)
        if not rows:
            return result
        positions = await bridge.get_positions()
        deals = await bridge.get_deal_history(7)
        orders = await _orders_held(bridge)
    except Exception as e:
        result["error"] = f"The broker could not be read ({e}). Nothing was written off."
        return result
    if positions is None or deals is None:
        result["error"] = "The broker could not be read. Nothing was written off."
        return result
    if orders is None:
        result["error"] = ("MetaTrader's order list could not be read, so an order still "
                           "on its way cannot be ruled out. Nothing was written off.")
        return result

    min_age = placeholder_single_no_fill_expiry_secs()
    for row in rows:
        trade_id = row["trade_id"]
        prefix = _comment_prefix(trade_id)
        try:
            live = next((p for p in positions
                         if str(p.get("comment") or "").startswith(prefix)), None)
            if live is not None:
                await _adopt_live_position(row, live)
                result["kept"].append({"trade_id": trade_id,
                                       "reason": "open at the broker; adopted"})
                continue
            if any(str(d.get("comment") or "").startswith(prefix) for d in deals):
                result["kept"].append({"trade_id": trade_id,
                                       "reason": "the broker has a deal for it"})
                continue
            if _holds_order(orders, prefix):
                result["kept"].append({"trade_id": trade_id,
                                       "reason": "MT5 still holds its order: sent and not yet "
                                                 "answered by the broker, so it may still fill"})
                continue
            if await _expire_never_filled(row, bridge, expiry_s=min_age):
                result["written_off"].append(trade_id)
            else:
                result["kept"].append({"trade_id": trade_id,
                                       "reason": "too new; its order may still be on its way"})
        except Exception as e:
            log.warning("[TemplateRepair] write-off of %s failed: %s", trade_id[:8], e)
            result["kept"].append({"trade_id": trade_id, "reason": str(e)})
    if result["written_off"]:
        log.warning("[TemplateRepair] owner wrote off %d unconfirmed placeholder(s): %s",
                    len(result["written_off"]), [t[:8] for t in result["written_off"]])
    return result


async def _expire_never_filled(row: dict, bridge: Any,
                               expiry_s: Optional[int] = None) -> bool:
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
    if age_s < (_expiry_for(row) if expiry_s is None else expiry_s):
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
        f"of it after {_age_text(age_s)}. No position was ever opened and "
        f"there is no P&L. It was holding one of your open-trade slots.",
        trade_id, "template_placeholder_no_fill_expired",
    ))
    return True


def _age_text(age_s: float) -> str:
    """"7m" or "26h": the single-mode expiry is minutes, and "0h" says
    nothing."""
    return f"{age_s / 60.0:.0f}m" if age_s < 3600 else f"{age_s / 3600.0:.0f}h"


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
    await _hand_to_ea(trade_id, ticket)
    return True


async def _hand_to_ea(trade_id: str, ticket: int) -> None:
    """Give an adopted position to the EA now, not at its next reconnect.

    Bug 070: when the EA's own market order timed out at the broker it
    reported the open as failed and forgot the trade. A later fill adopted
    here became an EA-managed row the EA knew nothing about, and Python skips
    EA-managed rows while the EA is healthy -- so nothing managed it until the
    EA's next hello ran _restore_open_trades.

    This is that same restore_trade, sent sooner. The EA ignores a ticket it
    already manages and reports one that has closed. Only when the row now
    carries THIS ticket (a fill event may have landed first and the guarded
    UPDATE skipped) and is EA-managed; an unhealthy EA is left to
    reclaim_ea_managed_trade. A failed send is logged, never raised: the
    adoption itself has already happened and must stand.
    """
    try:
        from backend.src.services.broker import ea_bridge
        ea = ea_bridge.get_instance()
        if ea is None or not ea.is_ea_healthy():
            return
        row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
        if (not row or row.get("managed_by") != "ea"
                or int(row.get("mt5_ticket") or 0) != int(ticket)):
            return
        await ea.restore_trade(row)
        log.info("[TemplateRepair] handed adopted trade=%s ticket=%s to the EA",
                 trade_id[:8], ticket)
    except Exception as e:
        log.warning("[TemplateRepair] could not hand trade=%s to the EA: %s — "
                    "its next reconnect will", trade_id[:8], e)


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
    # This runs to settle rows the other close paths left open, so of all five
    # callers it is the likeliest to arrive second. If it did, it stays quiet.
    if not result.get("already_closed"):
        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_trade_close(closed_row, result, {}, account),
            trade_id, f"template_placeholder_repair_{reason.lower()}",
        ))
    return True



