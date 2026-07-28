"""Four genuinely-computational blocks -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._monitor_loop, as part of
the core/engine.py migration series. See
docs/todo/refactor/core-monitor-loop-migration/020-*.md.

`_monitor_loop` itself (the master per-tick dispatcher) is NOT extracted as
a whole -- most of it is pure strategy-handler routing and cycle-counter-
gated delegation to already-extracted collaborators, the same "permanent
thin orchestration layer" judgment applied to `_handle_bot_command`
elsewhere in this series. These four pieces are the loop's only genuine,
previously-untested computation.

`reconcile_sl_hit`/`check_profit_close_target` can close a real MT5
position or record a partial close via the already-extracted
`core_close_trade.record_close`/`core_partial_close.partial_close_trade`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_close_trade import CloseTradeContext, record_close
from forex_trader.core.core_fees_sizing import pnl as _pnl
from forex_trader.core.core_partial_close import partial_close_trade

log = logging.getLogger(__name__)


def check_sl(trade: dict, tick: Any) -> Optional[tuple]:
    direction = trade["direction"].upper()
    sl = float(trade["stop_loss"]) if trade["stop_loss"] else None
    if direction == "BUY":
        if sl and tick.bid <= sl:
            return (trade["trade_id"], sl, "SL")
    else:
        if sl and tick.ask >= sl:
            return (trade["trade_id"], sl, "SL")
    return None


async def reconcile_sl_hit(trade: dict, tick: Any, price: float, reason: str,
                            bridge: Any, ctx: CloseTradeContext) -> str:
    """Reconciles a local SL crossing against MT5's own live position volume
    before trusting it. Returns "deferred" (broker's own SL hasn't fired
    yet -- do nothing this cycle), "partial" (broker already partially
    closed it -- a matching local partial close was recorded), or "closed"
    (a full local close was recorded, either because the ticket is fully
    gone at the broker or because the MT5 check couldn't be trusted)."""
    trade_id = trade["trade_id"]
    mt5_ticket = trade.get("mt5_ticket")
    if mt5_ticket and bridge.is_configured():
        try:
            live_pos = await bridge.get_positions()
            live_vol = {int(p["ticket"]): round(float(p.get("volume", 0)), 4)
                       for p in live_pos}
            if int(mt5_ticket) in live_vol:
                mt5_vol = live_vol[int(mt5_ticket)]
                app_rem = round(float(trade["remaining_lots"]), 4)
                if abs(mt5_vol - app_rem) < 0.001:
                    # Full position still open — MT5 SL hasn't fired yet
                    log.debug("[SL] ticket=%s price crossed SL but MT5 open — deferring",
                             mt5_ticket)
                    return "deferred"
                # MT5 partially closed — record partial, not full close
                closed_vol = round(app_rem - mt5_vol, 4)
                if closed_vol > 0.001:
                    partial_pnl = _pnl(
                        trade["direction"], float(trade["entry_price"]),
                        price, closed_vol,
                    )
                    try:
                        await partial_close_trade(trade_id, closed_vol, price, f"MT5_{reason}")
                    except Exception as _pe:
                        log.warning("[SL] partial_close_trade failed: %s", _pe)
                    asyncio.create_task(telegram_alerts.send_message(
                        telegram_alerts.fmt_mt5_partial_close(
                            trade, closed_vol, price, mt5_vol,
                            partial_pnl, reason,
                        ),
                        trade_id, f"mt5_partial_{reason.lower()}",
                    ))
                return "partial"
        except Exception as _ce:
            log.debug("[SL] MT5 check failed (%s), recording local close", _ce)

    result = await record_close(trade_id, price, reason, ctx)
    if mt5_ticket:
        asyncio.create_task(ctx.schedule_profit_sync(trade_id, int(mt5_ticket)))
    asyncio.create_task(ctx.background_close_commentary(trade_id, result, reason, tick))
    return "closed"


async def check_profit_close_target(trade: dict, tick: Any, profit_close_usd: float,
                                     bridge: Any, ctx: CloseTradeContext) -> bool:
    """Cumulative-P&L (realised partials + unrealised open) threshold check
    against `profit_close_usd`. Returns True if the trade was closed."""
    if profit_close_usd <= 0:
        return False
    cur = tick.bid if trade["direction"].upper() == "BUY" else tick.ask
    unrealized = _pnl(
        trade["direction"], float(trade["entry_price"]),
        cur, float(trade["remaining_lots"]),
    )
    realised = float(trade.get("realised_pnl") or 0.0)
    cumulative = realised + unrealized
    if cumulative < profit_close_usd:
        return False

    log.info(
        "[ProfitClose] %s hit $%.2f target "
        "(realised $%.2f + unrealised $%.2f = $%.2f cumulative)",
        trade["trade_id"], profit_close_usd,
        realised, unrealized, cumulative,
    )
    mt5_ticket = trade.get("mt5_ticket")
    close_price = cur
    if mt5_ticket:
        try:
            mt5_res = await bridge.close_position(int(mt5_ticket))
            if mt5_res.get("success"):
                close_price = float(mt5_res.get("close_price", cur))
        except Exception as _e:
            log.warning("Profit-close MT5 error: %s", _e)
    result = await record_close(trade["trade_id"], close_price, "profit_close_target", ctx)
    if mt5_ticket:
        asyncio.create_task(ctx.schedule_profit_sync(trade["trade_id"], int(mt5_ticket)))
    asyncio.create_task(ctx.background_close_commentary(
        trade["trade_id"], result, "profit_close_target", tick
    ))
    return True


async def reclaim_ea_managed_trade(trade: dict, strategy: str) -> bool:
    """Only meaningful when trade['managed_by'] == 'ea'. Returns True if the
    EA is healthy and management should stay with it (skip strategy
    handler dispatch this cycle); False if reclaimed by Python (DB updated,
    alert sent) and dispatch should proceed normally."""
    try:
        from forex_trader.core import ea_bridge as _ea_mod
        _ea = _ea_mod.get_instance()
        if _ea is not None and _ea.is_ea_healthy():
            return True
    except ImportError:
        pass

    # EA Template strategies must NEVER be reclaimed -- Python has no
    # handler for a "template:<name>" strategy at all (the monitor loop's
    # own dispatch falls straight through every named elif to the
    # scale_out default), and a template's grid/single-mode management,
    # Anchor TP ladder, breakeven, and trailing rules only exist in
    # ManageTemplate() on the EA side by design (core_ea_templates.py's own
    # docstring: "a template fully replaces strategy dispatch"). Confirmed
    # live 2026-07-27: a grid template trade got reclaimed here during a
    # brief EA reconnect, and the scale_out handler that then ran against
    # it used its still-zero entry_price (a grid placeholder pending its
    # first leg fill) against a live tick crossing tp1/tp2/tp3, fabricating
    # a $40,730 "profit" and closing the DB row with no real broker action
    # ever taken (mt5_ticket was 0, so the handler's own real-order guard
    # never fired) -- silently orphaning whatever the EA's actual resting/
    # filled legs were doing on the real account, with Python now blind to
    # them. Leaving managed_by as 'ea' here means dispatch keeps skipping
    # this trade every cycle until the EA reconnects -- no one manages it
    # in the gap, rather than the wrong thing managing it.
    from forex_trader.core.core_ea_templates import is_template_override
    if is_template_override(strategy):
        log.warning(
            "[EA] trade=%s ticket=%s strategy=%s EA unhealthy -- template "
            "strategies have no Python fallback, leaving unmanaged until "
            "the EA reconnects rather than reclaiming",
            trade["trade_id"][:8], trade.get("mt5_ticket"), strategy,
        )
        return True

    log.warning(
        "[EA] trade=%s ticket=%s EA unhealthy — reclaiming "
        "management in Python",
        trade["trade_id"][:8], trade.get("mt5_ticket"),
    )

    def _reclaim(tid=trade["trade_id"]):
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET managed_by='python' WHERE trade_id=?",
                (tid,),
            )
    await db_module.to_db_thread(_reclaim)
    asyncio.create_task(telegram_alerts.send_message(
        f"*EA Bridge Lost*\n"
        f"Ticket {trade.get('mt5_ticket')} ({strategy}) was being "
        f"managed by the local EA, which has stopped responding. "
        f"Management has been reclaimed by the app — no gap in "
        f"SL/TP coverage.",
        trade["trade_id"], "ea_bridge_lost",
    ))
    return False
