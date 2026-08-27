"""The panel's trade operations: market orders, cancelling pendings, moving a
channel to breakeven, pushing stops, and closing.

The two stop-loss recordings here go through panel_repo.record_stop_loss and
only after bridge.modify_order has accepted -- see that function's docstring
for why the order matters.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.src.db import database as db_module
from backend.src.services.broker import ea_templates
from backend.src.services.positions import panel_repo

from backend.src.services.positions._panel_shared import (
    MANUAL, MANUAL_SOURCE, Screen, _channel, _channel_open_trades,
    _trade_push_sl_pips,
)

log = logging.getLogger(__name__)


async def _market_order(slug: str, direction: str, ctx: Any) -> Screen:
    from backend.src.services.trading.manual_market_order import open_manual_market_order
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    tick = await ctx.get_tick()
    if tick is None:
        return Screen("No price available — is the MT5 bridge connected?", mode="send")

    # A market order needs a stop. A template channel states one (sl_pips); a
    # built-in-strategy channel does not, so fall back to the same DPM/ATR
    # path open_manual_market_order already uses when stop_loss is None
    # rather than inventing a distance here.
    stop_loss = None
    strategy = chan["strategy"]
    if chan["template"]:
        tpl = ea_templates.get_ea_template(chan["template"]) or {}
        sl_pips = float(tpl.get("sl_pips") or 0)
        if sl_pips > 0:
            price = tick.ask if direction == "BUY" else tick.bid
            stop_loss = price - sl_pips if direction == "BUY" else price + sl_pips
    try:
        result = await open_manual_market_order(
            ctx._bridge, direction,
            stop_loss=stop_loss,
            strategy=strategy or None,
            source_name=chan["name"] if chan["name"] != MANUAL else MANUAL_SOURCE,
            starting_balance=ctx._cfg.get("starting_balance", 1000.0),
            background_open_commentary=ctx._background_open_commentary,
        )
    except Exception as e:
        return Screen(f"*{direction} failed* — {e}", mode="send")
    entry = float(result.get("entry_price") or 0)
    return Screen(
        f"*{direction} placed* — {chan['name']}\n"
        f"Entry: {entry:.2f}  |  Lots: {result.get('lot_size', '?')}\n"
        f"MT5 Ticket: {result.get('mt5_ticket') or 'pending'}",
        mode="send")

async def _delete_pending(slug: str, ctx: Any) -> Screen:
    from backend.src.services.broker import ea_bridge as ea_mod
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    variants = [chan["name"], f"Telegram Auto ({chan['name']})"]
    marks = ",".join("?" for _ in variants)
    rows = panel_repo.working_pending_orders_for_sources(variants)
    if not rows:
        return Screen(toast="No working pending orders on this channel.", mode="noop")
    ea = ea_mod.get_instance()
    done, failed = 0, 0
    for row in rows:
        try:
            ok = await ea.cancel_pending_order(
                row["trade_id"], int(row.get("mt5_ticket") or 0), "panel_delete_pending")
            done += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception as e:
            log.warning("[Panel] cancel pending %s failed: %s", row.get("trade_id"), e)
            failed += 1
    return Screen(f"*Delete pending* — {chan['name']}\n"
                  f"Cancelled: {done}"
                  f"{f'  |  Failed: {failed}' if failed else ''}", mode="send")

async def _risk_free(slug: str, ctx: Any) -> Screen:
    """Move every open position on this channel to breakeven (SL = entry)."""
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    trades = _channel_open_trades(chan)
    if not trades:
        return Screen(toast="No open trades on this channel.", mode="noop")
    moved, skipped = [], 0
    for t in trades:
        ticket = int(t.get("mt5_ticket") or 0)
        entry = float(t.get("entry_price") or 0)
        if not ticket or not entry:
            skipped += 1          # still a staged template leg, nothing at the broker yet
            continue
        try:
            res = await ctx._bridge.modify_order(ticket, entry, None)
            if res.get("error"):
                skipped += 1
                continue
            panel_repo.record_stop_loss(t["trade_id"], entry)
            moved.append(ticket)
        except Exception as e:
            log.warning("[Panel] risk-free %s failed: %s", ticket, e)
            skipped += 1
    return Screen(f"*Risk free* — {chan['name']}\n"
                  f"SL moved to entry on {len(moved)} position(s)"
                  f"{f', {skipped} skipped' if skipped else ''}.", mode="send")

async def _close_channel(slug: str, ctx: Any) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    trades = _channel_open_trades(chan)
    if not trades:
        return Screen(toast="No open trades on this channel.", mode="noop")
    return Screen(await _close_many(trades, ctx, chan["name"]), mode="send")

async def _close_all(ctx: Any) -> Screen:
    from backend.src.services.analytics.reporting import get_open_trades
    trades = get_open_trades()
    if not trades:
        return Screen(toast="No open trades.", mode="noop")
    return Screen(await _close_many(trades, ctx, "all channels"), mode="send")

async def _close_many(trades: list, ctx: Any, label: str) -> str:
    lines = [f"*Closing {len(trades)} trade(s)* — {label}"]
    total = 0.0
    for t in trades:
        try:
            res = await ctx.close_trade(t["trade_id"], "manual_close")
            pnl = float(res.get("net_pnl", 0))
            total += pnl
            lines.append(f"{t.get('direction')} {t.get('lot_size')} @ "
                         f"{float(res.get('close_price', 0)):.2f}  "
                         f"P&L: {'+' if pnl >= 0 else ''}{pnl:.2f}")
        except Exception as e:
            lines.append(f"Failed {t.get('mt5_ticket') or t['trade_id'][:8]}: {e}")
    lines.append(f"Total P&L: {'+' if total >= 0 else ''}${total:.2f}")
    return "\n".join(lines)

async def _close_one(trade_prefix: str, ctx: Any) -> Screen:
    row = panel_repo.open_trade_by_prefix(trade_prefix)
    if not row:
        return Screen(toast="That trade is no longer open.", mode="noop")
    try:
        res = await ctx.close_trade(row["trade_id"], "manual_close")
    except Exception as e:
        return Screen(f"Close failed: {e}", mode="send")
    pnl = float(res.get("net_pnl", 0))
    return Screen(f"*Closed* {row.get('direction')} {row.get('lot_size')} @ "
                  f"{float(res.get('close_price', 0)):.2f}\n"
                  f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}", mode="send")

async def _push_sl_one(trade_prefix: str, ctx: Any) -> Screen:
    """manual_sl_push_pips / tg_cmd_enabled (2026-08-04 -- existed as
    template fields with no bot-command infrastructure to wire into at all;
    the old typed /commands were retired in favour of this button panel, so
    this is that panel's version rather than a new typed command). Nudges
    an EA Template trade's live broker SL by the template's own configured
    pip amount, same direct-modify pattern _risk_free above already uses
    for its own manual SL move, gated on tg_cmd_enabled per template."""
    row = panel_repo.open_trade_by_prefix(trade_prefix)
    if not row:
        return Screen(toast="That trade is no longer open.", mode="noop")

    push_pips = _trade_push_sl_pips(row)
    if push_pips <= 0:
        return Screen(toast="Push SL isn't available for this trade.", mode="noop")

    ticket = int(row.get("mt5_ticket") or 0)
    if not ticket:
        return Screen(toast="That leg hasn't filled yet.", mode="noop")

    positions = await ctx._bridge.get_positions()
    pos = next((p for p in positions if int(p.get("ticket") or 0) == ticket), None)
    if not pos:
        return Screen(toast="Couldn't read this position from the broker.", mode="noop")

    from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
    direction  = row.get("direction", "BUY")
    cur_sl     = float(pos.get("sl") or 0)
    cur_price  = float(pos.get("current_price") or 0)
    push_dist  = push_pips * PIPS_TO_PRICE_XAUUSD
    new_sl     = (cur_sl + push_dist) if direction == "BUY" else (cur_sl - push_dist)

    # A push that would land at or past current price is an invalid stop,
    # not a tighter one -- refuse rather than send a request the broker
    # would reject anyway (or, worse, one it fills as an instant close).
    landed_past_price = (
        (direction == "BUY" and new_sl >= cur_price) or
        (direction == "SELL" and new_sl <= cur_price)
    )
    if cur_sl <= 0 or landed_past_price:
        return Screen(toast="Push SL would land at/past current price — refused.", mode="noop")

    try:
        res = await ctx._bridge.modify_order(ticket, round(new_sl, 2), None)
        if res.get("error"):
            return Screen(f"Push SL failed: {res['error']}", mode="send")
    except Exception as e:
        return Screen(f"Push SL failed: {e}", mode="send")

    panel_repo.record_stop_loss(row["trade_id"], round(new_sl, 2))
    return Screen(f"*SL pushed* {row.get('direction')} {row.get('lot_size')} — "
                  f"new SL ${new_sl:.2f} (+{push_pips:.1f} pips)", mode="send")
