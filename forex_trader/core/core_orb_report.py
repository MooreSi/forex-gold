"""ORB/IVB auto-execute: places a genuine EA pending limit order at the
reload zone (2026-07-22) -- was previously a DB-only pending signal later
filled at market by the generic zone-fill watcher. No Python-bridge fallback
exists for this path, same as Limit Runner (core_limit_order_signal.py): if the
EA is not connected the setup is simply not captured that morning.

The report-building half moved to backend/src/services/analytics/orb_report.py
in phase 2; this function is a trading surface and moves with phase 8.
`is_active_trader_node` is taken as an explicit bool (the caller's
already-computed answer from SimulationEngine._is_active_trader_node) rather
than recomputed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from backend.src.utils.models import STRATEGY_ORB_FIXED

log = logging.getLogger(__name__)

_ORB_PENDING_EXPIRE_MINUTES = 60.0  # matches the pre-2026-07-22 zone-watcher's own ORB-specific expiry


async def orb_auto_execute(report: dict, bridge: Any, is_active_trader_node: bool) -> None:
    """
    Places the ORB/IVB Report's recommended trade unattended, every
    morning, when the auto-execute toggle is on.

    This scheduler runs independently and unconditionally on *both* Mac
    and VPS (same as the email report above it). Normally it deliberately
    does NOT forward to the other node the way the Trading tab's Execute
    Trade button does — that button forwards because it's a single user
    click on one specific node's UI with nowhere else to go, whereas here
    both nodes compute the same report on the same clock, so only the
    active trader should ever act, or the same trade fires twice.

    Under centralized signal generation (Settings > Remote Node), that
    inverts: the VPS has stopped analyzing entirely
    (should_generate_signals_here() is False there), so it must defer
    even though it's the active trader — and the Mac proceeds even
    though it *isn't* the active trader, since open_trade() (called via
    open_manual_market_order() below) forwards the resolved trade to the
    VPS for execution under that mode. Still exactly one node ever acts:
    whichever one currently owns generation.
    """
    _active_here = is_active_trader_node
    try:
        from forex_trader.sync import server as _sync_srv_mod
        _is_vps = _sync_srv_mod.get_instance() is not None
    except ImportError:
        _is_vps = False
    _centralized = bool((await db_module.to_db_thread(db_module.get_risk_settings))
                        .get("centralized_signal_gen_enabled"))
    if _is_vps:
        _proceed = _active_here and not _centralized
    else:
        _proceed = _active_here or _centralized
    if not _proceed:
        return

    direction = report.get("direction")
    if direction not in ("bullish", "bearish"):
        log.info("[ORB auto-execute] no breakout yet — skipping")
        return

    mt5_direction = "BUY" if direction == "bullish" else "SELL"
    rs = await db_module.to_db_thread(db_module.get_risk_settings)

    # Channel Strategy override (Trading > Strategy) -- same resolution
    # order as core_signal_resolution.resolve_open_trade_params: manual
    # override > auto-Claude rec > this channel's own default (orb_fixed,
    # not the global Active Strategy, since orb_fixed is what actually
    # suits a single-target breakout entry).
    from forex_trader.core import core_ea_templates as ea_templates
    _ch_override = await db_module.to_db_thread(
        db_module.get_channel_strategy_override, "ORB/IVB Report (auto)"
    )
    if _ch_override == "auto":
        _rec = await db_module.to_db_thread(
            db_module.get_channel_strategy_rec, "ORB/IVB Report (auto)"
        )
        strategy = _rec.get("strategy") or STRATEGY_ORB_FIXED
    elif _ch_override:
        strategy = _ch_override
    else:
        strategy = STRATEGY_ORB_FIXED

    if ea_templates.is_template_override(strategy):
        _tpl_name = ea_templates.template_name_from_override(strategy)
        log.info("[ORB auto-execute] channel assigned to EA Template '%s' -- not supported "
                 "for ORB/IVB's pending zone-entry order, skipping this morning", _tpl_name)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Skipped*\nChannel Strategy for 'ORB/IVB Report' is set to "
            f"EA Template '{_tpl_name}', which isn't supported for ORB/IVB's pending zone-entry "
            f"order (EA Templates only manage immediate-fill trades). Reassign it to a regular "
            f"strategy in Trading > Strategy > Channel Strategy.",
            None, "orb_auto_execute_skipped",
        ))
        return

    from forex_trader.core import ea_bridge as _ea_mod
    _ea = _ea_mod.get_instance()
    if _ea is None or not _ea.is_ea_healthy():
        log.info("[ORB auto-execute] EA not connected/healthy — setup not captured")
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Skipped*\nDirection: {mt5_direction}\n"
            f"EA not connected — no Python-bridge fallback for this strategy.",
            None, "orb_auto_execute_skipped",
        ))
        return

    # Near edge of the reload zone (the price side reached first as the
    # market retraces back into it after the breakout) — same convention
    # place_pending_order()'s other callers use, see core_limit_order_signal.py.
    price = report["entry_zone_high"] if mt5_direction == "BUY" else report["entry_zone_low"]
    stop_loss = report["stop"]
    target = report["target"]

    _lot_val = float(rs.get("orb_lot_size", 0) or 0)
    if _lot_val > 0:
        lot = _lot_val
    else:
        from forex_trader.core.core_close_trade import get_trading_balance
        from forex_trader.core.core_fees_sizing import suggest_lot_size
        balance = await get_trading_balance(bridge, 1000.0)
        lot = suggest_lot_size(price, stop_loss, balance, float(rs.get("risk_per_trade_pct", 0.5)))

    trade_id = str(uuid.uuid4())[:16]
    try:
        ack = await _ea.place_pending_order(
            trade_id, mt5_direction, price, lot, stop_loss,
            {1: target}, [1.0], 0, strategy,
            expire_minutes=_ORB_PENDING_EXPIRE_MINUTES, close_full_on_last=True,
        )
    except Exception as e:
        log.warning("[ORB auto-execute] place_pending_order failed: %s", e)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Failed*\nDirection: {mt5_direction}\nError: {e}",
            None, "orb_auto_execute_failed",
        ))
        return

    if ack.get("type") != "pending_order_placed":
        err = ack.get("error", "unknown error")
        log.warning("[ORB auto-execute] EA rejected pending order: %s", err)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Rejected*\nDirection: {mt5_direction}\nError: {err}",
            None, "orb_auto_execute_rejected",
        ))
        return

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "ORB/IVB Report (auto)", mt5_direction,
             report["entry_zone_low"], report["entry_zone_high"], stop_loss, target, lot,
             f"ORB/IVB pending order @ {price:.2f} (EA ticket {ticket})",
             "pending", now),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, None, "ORB/IVB Report (auto)", mt5_direction, price, stop_loss,
             json.dumps({1: target}), json.dumps([1.0]), 0, 0, lot, ticket,
             "working", now, strategy),
        )
    log.info(
        "[ORB auto-execute] pending %s ticket=%s @ %.2f stop=%.2f target=%.2f lot=%.2f",
        mt5_direction, ticket, price, stop_loss, target, lot,
    )
    asyncio.create_task(telegram_alerts.send_message(
        f"*ORB/IVB Limit Order Placed*\nDirection: {mt5_direction}\n"
        f"Price: {price:.2f}\nStop: {stop_loss:.2f}\nTarget: {target:.2f}\n"
        f"Lot: {lot:g}\nEA ticket: {ticket}",
        None, "orb_auto_execute_placed",
    ))
