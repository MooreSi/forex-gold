"""ORB Fixed TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_orb_fixed, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-orb-fixed-handler-migration/020-*.md.

Calls bridge.partial_close -- a real MT5 order-close call, unchanged from
the original. This module closes no order itself; it only calls whatever
`bridge` its caller supplies.

Takes `bridge` and a TPCache (pack 5) explicitly instead of self._bridge/
self._tp_cache. Reuses core_tp_trigger_tracking.check_tp_hits/
get_remaining_lots (pack 5), core_partial_close.partial_close_trade
(pack 9).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_tp_trigger_tracking import TPCache, check_tp_hits, get_remaining_lots
from backend.src.utils.models import Tick

log = logging.getLogger(__name__)


async def handle_orb_fixed(trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache) -> None:
    """
    ORB/IVB Report trades — no management beyond the reload-zone setup
    itself. SL is already handled generically by _check_sl() in
    _monitor_loop before any strategy handler ever runs; this handler's
    only job is a full close (no partials, no BE-move, no trailing) the
    instant tp1 (the report's own target) is hit. Deliberately bypasses
    DPM even when the global DPM toggle is on — see the dispatch order in
    _monitor_loop — since the whole point of this strategy is "exactly
    the setup the report computed, nothing recalculated."
    """
    hits = await check_tp_hits(tp_cache, trade, tick)
    if not hits:
        return
    trade_id   = trade["trade_id"]
    mt5_ticket = trade.get("mt5_ticket")
    tp1_val    = float(trade["tp1"])
    remaining  = await db_module.to_db_thread(get_remaining_lots, trade_id)
    if remaining <= 0:
        return

    actual_price = tp1_val
    if mt5_ticket:
        mt5_res = await bridge.partial_close(int(mt5_ticket), remaining)
        if mt5_res.get("success"):
            actual_price = float(mt5_res.get("close_price", tp1_val))
        elif mt5_res.get("error") or mt5_res.get("success") is False:
            log.warning("[orb_fixed] MT5 target close failed ticket=%s: %s",
                        mt5_ticket, mt5_res)
            return

    result = await partial_close_trade(trade_id, remaining, actual_price, "Target")
    asyncio.create_task(telegram_alerts.send_message(
        telegram_alerts.fmt_tp_hit(trade, 1, actual_price, remaining,
                                    result.get("partial_pnl", 0)),
        trade_id, "orb_fixed_target",
    ))
    log.info("[orb_fixed] %s target @ %.2f — full close", trade_id[:8], actual_price)
    log.info("[orb_fixed] %s target @ %.2f — full close", trade_id[:8], actual_price)
