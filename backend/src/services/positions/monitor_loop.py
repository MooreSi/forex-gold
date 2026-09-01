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

from backend.src.db import database as db_module
from backend.src.services.positions import repo as positions_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.close_trade import CloseTradeContext, record_close
from backend.src.services.trading.fees_sizing import pnl as _pnl
from backend.src.services.trading.partial_close import partial_close_trade

log = logging.getLogger(__name__)

from backend.src.utils import log_throttle as _throttle


def check_sl(trade: dict, tick: Any) -> Optional[tuple]:
    direction = trade["direction"].upper()
    # A row with no entry price has no fill behind it yet -- an EA Template
    # placeholder waiting for a leg to go live (mt5_ticket=0, entry_price=0,
    # see ea_bridge._promote_leg_fill). Closing one here recorded a DB-only
    # "Stop Loss Hit" with a P&L computed off a zero entry while the EA's real
    # position was still running (live, trade 76687f1a, 2026-07-29). Nothing
    # about an unfilled placeholder can hit a stop.
    if not float(trade.get("entry_price") or 0):
        return None
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
            live_pos = await bridge.get_positions() or []
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


def _report_close_refused(trade: dict, detail: str) -> None:
    """A broker close that did not happen must not become a database close.

    Loud, because the old behaviour was a debug/warning line and then a
    phantom close row -- which looks exactly like a normal successful close in
    every screen that reads the database. Never raises: this runs inside the
    monitor loop, and losing position management is worse than losing an
    alert.
    """
    # The close is retried on every tick while the target is still met, so a
    # standing refusal logged in full each time is an ERROR every second --
    # measured at exactly that on 2026-09-01 while AutoTrading was off.
    # Retrying is right; saying so a thousand times is not.
    # One decision for both the log and the alert. On 2026-09-01 this sent 45
    # identical push notifications for one trade in 45 seconds, because the
    # log was throttled and the alert deliberately was not -- "a message to
    # the operator is not log noise". True of the first one; the 46th is not
    # information, it is the operator's phone being used against him.
    #
    # Retrying the close is still right: the target is still met and
    # AutoTrading may come back. Saying so every second is not.
    _loud = _throttle.should_announce(
        f"close-refused:{trade.get('trade_id', '')}", detail,
    )
    (log.error if _loud else log.debug)(
        "[Close] trade=%s ticket=%s NOT closed — %s. Leaving it open in the "
        "database; reconciliation will settle it.",
        str(trade.get("trade_id", ""))[:8], trade.get("mt5_ticket"), detail,
    )
    if not _loud:
        return
    try:
        asyncio.create_task(telegram_alerts.send_message(
            f"*Close refused by the broker*\n"
            f"Ticket {trade.get('mt5_ticket')} was not closed: {detail}\n"
            f"The trade is still open and still managed. Nothing has been "
            f"recorded as closed.",
            str(trade.get("trade_id", "")), "close_refused",
        ))
    except Exception:
        pass


async def check_profit_close_target(trade: dict, tick: Any, profit_close_usd: float,
                                     bridge: Any, ctx: CloseTradeContext) -> bool:
    """Cumulative-P&L (realised partials + unrealised open) threshold check
    against `profit_close_usd`. Returns True if the trade was closed."""
    if profit_close_usd <= 0:
        return False
    # Same placeholder guard as check_sl() -- unrealised P&L measured from a
    # zero entry price is contract-value-sized and would trip any target.
    if not float(trade.get("entry_price") or 0):
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
        # stage3/040. record_close used to run whatever happened here: a
        # success=False was never checked at all, and an exception was caught
        # and warned. Either way the database said closed while MT5 still held
        # the position -- the worst shape of wrong, because the app then stops
        # managing a trade that is still open and moving, and books a P&L that
        # never happened.
        #
        # Only a CONFIRMED broker close may be recorded. Anything else leaves
        # the row open, says so loudly, and lets reconciliation (030) settle
        # it -- the trade stays managed in the meantime.
        try:
            mt5_res = await bridge.close_position(int(mt5_ticket))
        except Exception as _e:
            _report_close_refused(trade, f"broker close raised: {_e}")
            return False
        if not (mt5_res or {}).get("success"):
            _report_close_refused(
                trade, f"broker refused the close: {(mt5_res or {}).get('error')}")
            return False
        close_price = float(mt5_res.get("close_price", cur))
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
        from backend.src.services.broker import ea_bridge as _ea_mod
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
    from backend.src.services.broker.ea_templates import is_template_override
    if is_template_override(strategy):
        # Once per trade while the condition holds, then hourly. This runs on
        # every monitor cycle -- once a second while trades are open -- and on
        # 2026-09-01 it produced ~400 identical lines in the seven minutes an
        # EA was off the chart. It must not go silent either: an unmanaged
        # template position is bugs/013 and is the worst state in the app.
        if _throttle.should_announce(
            f"ea-unhealthy:{trade['trade_id']}", f"template:{strategy}",
        ):
            log.warning(
                "[EA] trade=%s ticket=%s strategy=%s EA unhealthy -- template "
                "strategies have no Python fallback, leaving unmanaged until "
                "the EA reconnects rather than reclaiming",
                trade["trade_id"][:8], trade.get("mt5_ticket"), strategy,
            )
        return True

    # Reclaiming is a one-off transition, not a standing condition -- but the
    # caller can reach here repeatedly if the handoff keeps flapping, so it is
    # throttled on the same key. Clearing on recovery is what makes a genuine
    # flap visible instead of collapsing into one line.
    if _throttle.should_announce(
        f"ea-unhealthy:{trade['trade_id']}", "reclaimed",
    ):
        log.warning(
            "[EA] trade=%s ticket=%s EA unhealthy — reclaiming "
            "management in Python",
            trade["trade_id"][:8], trade.get("mt5_ticket"),
        )

    await db_module.to_db_thread(
        positions_repo.reclaim_management, trade["trade_id"])
    asyncio.create_task(telegram_alerts.send_message(
        f"*EA Bridge Lost*\n"
        # Escaped: strategy names carry a single underscore (be_runner,
        # scalp_runner, trail_stop, scale_out), which Markdown reads as an
        # unbalanced italic delimiter. This alert had been rejected by
        # Telegram since at least 2026-08-26 for exactly that.
        f"Ticket {trade.get('mt5_ticket')} ({telegram_alerts._md_esc(strategy)}) was being "
        f"managed by the local EA, which has stopped responding. "
        f"Management has been reclaimed by the app — no gap in "
        f"SL/TP coverage.",
        trade["trade_id"], "ea_bridge_lost",
    ))
    return False
