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

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.telegram import alerts as telegram_alerts
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
    though it *isn't* the active trader, since open_manual_market_order()
    below forwards nothing itself (it acts on whichever bridge it's
    given) -- the resolved trade lands wherever this function runs. Still
    exactly one node ever acts: whichever one currently owns generation.

    2026-08-01: places a genuine immediate MARKET order (matching the
    manual Execute button), not a resting pending-limit order -- there is
    no reload/pullback zone in the rebuilt methodology to rest an order
    at.
    """
    _active_here = is_active_trader_node
    try:
        from backend.src.services.cluster.sync import server as _sync_srv_mod
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
        log.info("[ORB auto-execute] no confirmed breakout yet — skipping")
        return

    stop_loss = report.get("stop")
    target = report.get("target")
    if not stop_loss or not target:
        log.info("[ORB auto-execute] report has no stop/target — skipping")
        return

    # Stop/target are fixed the moment the opening range closes (breakout
    # edge ± a multiple of that range's own risk) and never move again --
    # correct for the methodology, but this function can run anywhere from
    # 08:15 to _ORB_AUTO_EXEC_END_HOUR, and only fires on whichever minute
    # first sees a confirmed direction (e.g. after this node's own engine
    # restarted mid-morning and only just started evaluating the report).
    # If price has already reached or passed the target by the time that
    # happens, the 2:1 reward this trade is supposed to be chasing has
    # already played out with no position open -- entering now would open
    # a trade whose own "target" sits behind the entry, not ahead of it
    # (confirmed live 2026-08-03: a SELL filled at $4038.12 against a
    # target of $4053.94, ~15pts on the wrong side of its own objective).
    # Skip rather than take a trade with no reward left.
    current_price = report.get("current_price")
    if current_price is not None:
        target_already_passed = (
            (direction == "bullish" and current_price >= target)
            or (direction == "bearish" and current_price <= target)
        )
        if target_already_passed:
            log.info(
                "[ORB auto-execute] price %.2f has already reached/passed the %s target "
                "%.2f — breakout is too stale to trade, skipping",
                current_price, direction, target,
            )
            asyncio.create_task(telegram_alerts.send_message(
                f"*ORB/IVB Auto-Execute Skipped*\nDirection: "
                f"{'BUY' if direction == 'bullish' else 'SELL'}\n"
                f"Price (${current_price:.2f}) has already reached/passed the target "
                f"(${target:.2f}) before the trade could be placed — the breakout is too "
                f"stale, no reward left to take.",
                None, "orb_auto_execute_skipped",
            ))
            return

    mt5_direction = "BUY" if direction == "bullish" else "SELL"
    rs = await db_module.to_db_thread(db_module.get_risk_settings)

    # Channel Strategy override (Trading > Strategy) -- same resolution
    # order as core_signal_resolution.resolve_open_trade_params: manual
    # override > auto-Claude rec > this channel's own default (orb_fixed,
    # not the global Active Strategy, since orb_fixed is what actually
    # suits a single-target breakout entry).
    from backend.src.services.broker import ea_templates as ea_templates
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
                 "for ORB/IVB's market entry, skipping this morning", _tpl_name)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Skipped*\nChannel Strategy for 'ORB/IVB Report' is set to "
            f"EA Template '{_tpl_name}', which isn't supported for ORB/IVB's market entry "
            f"(EA Templates only manage immediate-fill trades opened through the normal signal "
            f"path, not this report). Reassign it to a regular strategy in Trading > Strategy > "
            f"Channel Strategy.",
            None, "orb_auto_execute_skipped",
        ))
        return

    # 0 (unset) is documented (Trading > ORB/IVB Report tab) as "auto-size
    # from your Risk % and the stop distance". Passing lot_size=None down
    # to open_manual_market_order does NOT do that, though -- its own
    # fallback ladder tries the GENERIC strategy_lot_size setting first and
    # only reaches real risk-based sizing if that is also 0, so an ORB
    # install that never set orb_lot_size (e.g. a fresh reinstall, which
    # doesn't carry over per-node settings) silently traded whatever the
    # unrelated global strategy_lot_size happened to be instead of sizing
    # to this trade's own stop distance. Compute the risk-based lot here so
    # ORB's documented behaviour actually holds regardless of what the
    # generic strategy lot is set to.
    _lot_val = float(rs.get("orb_lot_size", 0) or 0)
    if _lot_val > 0:
        lot = _lot_val
    else:
        from backend.src.services.trading.close_trade import get_trading_balance
        from backend.src.services.trading.fees_sizing import suggest_lot_size
        _entry_approx = float(report.get("current_price") or 0) or stop_loss
        _risk_pct = float(rs.get("risk_per_trade_pct", 0.5))
        _balance = await get_trading_balance(bridge, 1000.0)
        lot = suggest_lot_size(_entry_approx, stop_loss, _balance, _risk_pct)

    from backend.src.services.trading.manual_market_order import open_manual_market_order
    try:
        result = await open_manual_market_order(
            bridge, mt5_direction, stop_loss=stop_loss, lot_size=lot,
            strategy=strategy, take_profit=target, source_name="ORB/IVB Report (auto)",
        )
    except Exception as e:
        log.warning("[ORB auto-execute] open_manual_market_order failed: %s", e)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Failed*\nDirection: {mt5_direction}\nError: {e}",
            None, "orb_auto_execute_failed",
        ))
        return

    entry = float(result.get("entry_price", 0) or 0)
    ticket = result.get("mt5_ticket", "—")
    log.info(
        "[ORB auto-execute] %s filled @ %.2f stop=%.2f target=%.2f ticket=%s",
        mt5_direction, entry, stop_loss, target, ticket,
    )
    asyncio.create_task(telegram_alerts.send_message(
        f"*ORB/IVB Market Order Placed*\nDirection: {mt5_direction}\n"
        f"Entry: {entry:.2f}\nStop: {stop_loss:.2f}\nTarget: {target:.2f}\n"
        f"EA ticket: {ticket}",
        None, "orb_auto_execute_placed",
    ))
