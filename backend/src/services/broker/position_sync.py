"""Reconciling app trades against what the broker actually still holds (M4 B9c).

This was SimulationEngine._sync_closed_mt5_positions: detect tickets that
vanished from MT5, require a miss streak before believing it, record the
close, sync the realised profit, and import positions opened directly in
the terminal.

Relocation only. The body below is the original verbatim; the nine `self.`
references became PositionSyncCtx fields and nothing else changed -- no
argument added, removed, defaulted or reordered on any close-path call.
That restraint is the point: this code decides that a real trade has
closed, and reshaping it needs a demo-account session and sign-off, which
this refactor deliberately does not have.

Two ctx fields are shared with the runtime by reference rather than
copied, and both would fail subtly if copied:

  - mt5_sync_missing_streak counts CONSECUTIVE cycles a ticket has been
    missing. A per-call copy resets the count every cycle, so every
    transient broker hiccup would read as a real close.
  - miss_threshold is the tuning constant that streak is compared against.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import asyncio
import re
import time
import uuid

from backend.src.utils.models import STRATEGY_SCALE_OUT
from backend.src.services.broker import repo as _broker_repo
from backend.src.services.positions.tp_tracking import last_closed_tp as _last_closed_tp_impl
from backend.src.db import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)


@dataclass
class PositionSyncCtx:
    """Everything the reconciliation loop reached for through `self`."""
    bridge: Any = None
    # Shared by reference -- see the module docstring.
    mt5_sync_missing_streak: dict = field(default_factory=dict)
    miss_threshold: int = 2
    # Bound runtime methods. The last four are close-path operations and
    # are passed through untouched.
    get_tick: Optional[Callable[..., Awaitable[Any]]] = None
    partial_close_trade: Optional[Callable[..., Awaitable[dict]]] = None
    record_close: Optional[Callable[..., Awaitable[dict]]] = None
    sync_profit: Optional[Callable[..., Awaitable[Optional[float]]]] = None
    schedule_profit_sync: Optional[Callable[..., Awaitable[None]]] = None
    get_mt5_account: Optional[Callable[[], Awaitable[dict]]] = None


async def sync_closed_mt5_positions(ctx: PositionSyncCtx) -> None:
    if not ctx.bridge.is_configured():
        return
    def _fetch_open_trades():
            # Excludes managed_by='ea' trades: the native EA already pushes
            # its own trade_closed event the moment it detects the position
            # gone (ea_bridge.py's _on_trade_closed), which calls the same
            # _record_close() + sends the same Telegram alert this function
            # would otherwise send a second time. _record_close() has no
            # idempotency guard, so without this exclusion both paths race
            # to close the trade and the user gets a duplicate "Stop Loss
            # Hit" message (confirmed live, ticket 1572181515, 2026-07-10 —
            # single DB row, single node, no dual-node involvement at all).
            # Non-EA-managed trades still need this poll as their only
            # detection of an out-of-band MT5-side close.
            # Also excludes any trade with vantage_ladder_legs rows (Adaptive
            # Runner ladder trades): this trade's own mt5_ticket is only
            # leg 1/the anchor, so once leg 1 closes at its own native TP
            # this loop would see the anchor ticket vanish, compute
            # closed_volume from JUST that leg, and — since
            # _handle_adaptive_runner_ladder had already subtracted leg 1's
            # lots from remaining_lots — call partial_close_trade() a
            # SECOND time for the same lots every monitor cycle (the
            # ticket never reappears, so the miss-streak keeps re-firing),
            # draining remaining_lots to 0 and marking the whole parent
            # trade closed within seconds even though legs 2-N are still
            # genuinely open in MT5. Once the parent shows status!='open',
            # _handle_adaptive_runner_ladder (which owns real per-leg
            # closure detection AND survivor SL-trailing) stops being
            # invoked at all for it, orphaning the remaining legs from
            # all further management. Confirmed live 2026-07-17: trades
            # b7dcacbe/bed873ca both closed within ~30s of leg 1, legs
            # 2-N left untracked until a SEPARATE bug (the untracked-
            # position importer not checking vantage_ladder_legs; also
            # fixed below) re-discovered them as phantom duplicate trades.
            return _broker_repo.fetch_python_managed_open_trades()
    open_trades = await db_module.to_db_thread(_fetch_open_trades)
    if not open_trades:
        return
    live_positions = await ctx.bridge.get_positions()

    # None means the read itself failed -- see the note in mt5_client.py. This
    # function decides whether a trade has CLOSED, so a failed read must never
    # be mistaken for "no positions": that closes every live trade in the
    # database and fires a Telegram alert for each.
    #
    # Until 2026-08-31 the clients could not report a failed read at all, and
    # the health check below was the workaround for the ambiguity. It stays,
    # because a genuinely empty list from a disconnected-but-responding bridge
    # is still ambiguous.
    if live_positions is None:
        log.debug("MT5 sync: skipping — the position read failed")
        return

    if not live_positions:
        health = await ctx.bridge.get_health()
        if not health.get("connected", False):
            log.debug("MT5 sync: skipping — bridge not connected (live_positions empty)")
            return

    live_tickets = {int(p["ticket"]) for p in live_positions}
    deals_by_pos: dict[int, list] = {}
    all_deals = await ctx.bridge.get_deal_history(7) or []
    for d in all_deals:
        pid = d.get("position_id")
        if pid:  # excludes None and 0
            deals_by_pos.setdefault(int(pid), []).append(d)
    tick = await ctx.get_tick()
    for trade in open_trades:
        ticket = int(trade["mt5_ticket"])
        if ticket in live_tickets:
            ctx.mt5_sync_missing_streak.pop(trade["trade_id"], None)
            continue

        # A ticket can be transiently absent from get_positions() (bridge
        # lock contention, a momentary IPC hiccup) without the position
        # actually having closed. Require MT5_SYNC_MISS_THRESHOLD
        # consecutive misses before acting, so one bad read can't
        # falsely mark a genuinely-open trade as closed.
        streak = ctx.mt5_sync_missing_streak.get(trade["trade_id"], 0) + 1
        ctx.mt5_sync_missing_streak[trade["trade_id"]] = streak
        if streak < ctx.miss_threshold:
            log.warning(
                "MT5 sync: ticket=%s missing from live positions (%d/%d) — "
                "not yet treating as closed",
                ticket, streak, ctx.miss_threshold,
            )
            continue

        deals = await ctx.bridge.get_position_history(ticket) or []
        if not deals:
            deals = deals_by_pos.get(ticket, [])
        close_price = None
        reason = "MT5_close"
        close_deals: list = []
        if deals:
            # entry 1=OUT, 2=INOUT, 3=OUT_BY (close-by-opposite on hedge accounts)
            close_deals = [d for d in deals if d.get("entry") in (1, 2, 3)]
            if not close_deals:
                open_type = 0 if trade["direction"].upper() == "BUY" else 1
                close_deals = [d for d in deals if d.get("type") != open_type]
            if close_deals:
                best = max(close_deals, key=lambda d: d.get("time", 0))
                close_price = best.get("price")
                comment = (best.get("comment") or "").lower()
                if "sl" in comment or "stop" in comment:
                    reason = "SL"
                elif "tp" in comment or "take" in comment:
                    reason = "MT5_sync_TP"
        if close_price is None:
            close_price = (tick.bid if trade["direction"].upper() == "BUY" else tick.ask) if tick \
                else float(trade.get("entry_price") or 0)
        try:
            # ── Partial-close detection ───────────────────────────────────────
            # If MT5 closed fewer lots than we are tracking, record a partial
            # close and keep the trade open rather than falsely marking it done.
            if close_deals:
                closed_volume = round(
                    sum(float(d.get("volume", 0)) for d in close_deals), 4
                )
                remaining_lots = round(float(trade["remaining_lots"]), 4)
                if closed_volume < remaining_lots - 0.001:
                    partial_profit = round(sum(
                        float(d.get("profit", 0)) + float(d.get("swap", 0))
                        + float(d.get("fee", 0))
                        for d in close_deals
                    ), 2)
                    log.info(
                        "MT5 sync: partial close trade=%s ticket=%s "
                        "closed=%.4f remaining=%.4f profit=%.2f",
                        trade["trade_id"], ticket, closed_volume,
                        remaining_lots - closed_volume, partial_profit,
                    )
                    # `reason` is already "MT5_close"/"MT5_sync_TP" in two of
                    # its three possible values (only "SL" isn't) -- blindly
                    # prefixing produced "MT5_MT5_close"/"MT5_MT5_sync_TP".
                    await ctx.partial_close_trade(
                        trade["trade_id"], closed_volume, float(close_price),
                        reason if reason.startswith("MT5_") else f"MT5_{reason}",
                    )
                    # Update ticket if the continuing position has a new ticket
                    new_remaining = round(remaining_lots - closed_volume, 4)
                    for lp in live_positions:
                        lp_vol = round(float(lp.get("volume", 0)), 4)
                        lp_ticket = int(lp.get("ticket", 0))
                        if abs(lp_vol - new_remaining) < 0.001 and lp_ticket != ticket:
                            await db_module.to_db_thread(
                                _broker_repo.reassign_mt5_ticket,
                                trade["trade_id"], lp_ticket)
                            log.info("MT5 sync: ticket %s → %s (partial close continues)",
                                     ticket, lp_ticket)
                            break
                    asyncio.create_task(telegram_alerts.send_message(
                        telegram_alerts.fmt_mt5_partial_close(
                            trade, closed_volume, float(close_price),
                            new_remaining, partial_profit, reason,
                        ),
                        trade["trade_id"], f"mt5_partial_{reason.lower()}",
                    ))
                    ctx.mt5_sync_missing_streak.pop(trade["trade_id"], None)
                    continue  # trade still open — do not record as full close

            # ── Full close ────────────────────────────────────────────────────
            ctx.mt5_sync_missing_streak.pop(trade["trade_id"], None)
            log.info("MT5 sync: closing trade %s ticket=%s @ %.2f reason=%s",
                     trade["trade_id"], ticket, close_price, reason)
            result = await ctx.record_close(trade["trade_id"], float(close_price), reason)
            await ctx.sync_profit(trade["trade_id"], ticket)
            asyncio.create_task(ctx.schedule_profit_sync(trade["trade_id"], ticket))
            closed_row = await db_module.to_db_thread(
                _broker_repo.fetch_trade, trade["trade_id"])
            account  = await ctx.get_mt5_account()
            last_tp  = await db_module.to_db_thread(_last_closed_tp_impl, trade["trade_id"]) if reason == "SL" else None
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(closed_row, result, {}, account,
                                                last_tp=last_tp),
                trade["trade_id"], f"mt5_sync_{reason}",
            ))
        except Exception as e:
            log.warning("MT5 sync close failed %s: %s", trade["trade_id"], e)

    # ── Import any MT5 positions the app doesn't know about ───────────────
    # Covers trades opened directly in MT5 and positions where a partial
    # close on a hedge account replaced the ticket with a new one.
    def _fetch_known_tickets():
        return _broker_repo.fetch_known_mt5_tickets()
    try:
        all_known_tickets = await db_module.to_db_thread(_fetch_known_tickets)
    except Exception:
        all_known_tickets = set()

    rs = await db_module.to_db_thread(db_module.get_risk_settings)
    default_strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT) or STRATEGY_SCALE_OUT

    for pos in live_positions:
        ticket = int(pos["ticket"])
        if ticket in all_known_tickets:
            continue
        # New untracked position — import it so the engine can manage it
        trade_id = str(uuid.uuid4())[:16]
        direction = pos.get("type", "BUY").upper()
        lot_size  = float(pos.get("volume", 0.01))
        entry_p   = float(pos.get("open_price", 0))
        sl        = float(pos.get("sl") or 0) or None
        tp        = float(pos.get("tp") or 0) or None
        open_ts   = float(pos.get("open_time") or time.time())
        try:
            # Realised-R inputs, captured at import (upstream 8d20bd3).
            _init_risk = (round(abs(entry_p - sl) * lot_size * CONTRACT_SIZE, 4)
                          if sl else None)
            await db_module.to_db_thread(
                _broker_repo.import_direct_mt5_position,
                trade_id, ticket, direction, entry_p, lot_size, sl, tp,
                open_ts, default_strategy, time.time(),
                sl or None, _init_risk)
            log.info(
                "MT5 sync: imported untracked position ticket=%s %s %.2f lots @ %.2f",
                ticket, direction, lot_size, entry_p,
            )
            all_known_tickets.add(ticket)
        except Exception as imp_err:
            log.warning("MT5 sync: failed to import ticket %s: %s", ticket, imp_err)
