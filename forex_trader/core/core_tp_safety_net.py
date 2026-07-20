"""TP safety net -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine._tp_safety_net_sweep/
_tp_safety_net_check_trade/_compute_be_cost_pts, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-tp-safety-net-migration/020-*.md.

Calls bridge.modify_order -- a real MT5 order-modify call, unchanged from
the original. This module modifies no order itself; it only calls
whatever `bridge` its caller supplies.

`last_alert` (per-trade cooldown-timestamp dict) is taken as an explicit
parameter -- instance state that isn't derivable from the database, same
pattern as `retry_after`/`missing_streak` in the two prior background-loop
packs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_trade_reporting import get_open_trades
from forex_trader.core.models import STRATEGY_BE_RUNNER, STRATEGY_GD_VIP_RUNNER, STRATEGY_SCALP_RUNNER

log = logging.getLogger(__name__)

_TP_SAFETY_NET_ALERT_COOLDOWN = 1800  # min seconds between repeat alerts for the same trade


async def tp_safety_net_sweep(bridge: Any, last_alert: dict) -> None:
    open_trades = get_open_trades()
    if not open_trades:
        return
    now = time.time()
    for trade in open_trades:
        try:
            await tp_safety_net_check_trade(trade, now, bridge, last_alert)
        except Exception as e:
            log.debug("[TP-SafetyNet] %s check failed: %s", trade.get("trade_id", "?")[:8], e)


async def tp_safety_net_check_trade(trade: dict, now: float, bridge: Any, last_alert: dict) -> None:
    # be_runner sets a real TP on the MT5 ticket and lets the broker
    # manage the exit — nothing for this loop to protect there.
    if trade.get("strategy") == STRATEGY_BE_RUNNER:
        return
    if bool(trade.get("sl_moved_to_be")):
        return  # already protected, by the live loop or a prior sweep
    # EA-managed trades are protected by the EA's own on-tick logic (and
    # kept in sync via update_trade()) — this sweep must not also touch
    # the broker-side SL for them. Confirmed live on ticket 1556670216:
    # this check was missing, so this loop moved an EA-managed trade's SL
    # to breakeven+cost out from under the EA using the wrong TP trigger
    # (see be_tp_field below) with no coordination between the two.
    if trade.get("managed_by") == "ea":
        try:
            from forex_trader.core import ea_bridge as _ea_mod
            _ea = _ea_mod.get_instance()
            if _ea is not None and _ea.is_ea_healthy():
                return
        except Exception:
            pass
    # Which TP is the real breakeven trigger for this strategy — must match
    # the strategy's own handler, or this safety net would "protect" a
    # trade at the wrong TP. GD VIP Runner and Scalp Runner both
    # deliberately keep their wider entry SL until TP2 (see
    # _handle_gd_vip_runner / _handle_scalp_runner) — confirmed live on
    # ticket 1556670216, where scalp_runner defaulting to TP1 here caused
    # a premature breakeven lock after only TP1 territory was touched.
    be_tp_field = "tp2" if trade.get("strategy") in (STRATEGY_GD_VIP_RUNNER, STRATEGY_SCALP_RUNNER) else "tp1"
    be_tp_label = be_tp_field.upper()
    be_trigger_tp = trade.get(be_tp_field)
    if be_trigger_tp is None:
        return
    open_time = float(trade.get("open_time") or 0)
    # Was 120s — a genuine live-loop miss (a gap/stall skipping a TP
    # crossing) is exactly as possible 5 seconds after open as 5 minutes
    # after; there's no real grace period the live handler needs. This
    # floor only needs to cover the fill settling into the DB and one M1
    # bar existing to check against — a few seconds, not two minutes.
    if not open_time or (now - open_time) < 15:
        return  # too young — fill may not be settled / no bar to check yet

    direction = (trade.get("direction") or "").upper()
    candles = await bridge.get_candles_range(open_time, now, "M1")
    if not candles:
        return
    if direction == "BUY":
        extreme = max(float(c.get("high", 0) or 0) for c in candles)
        reached = extreme >= float(be_trigger_tp)
    else:
        extreme = min(float(c.get("low", float("inf")) or float("inf")) for c in candles)
        reached = extreme <= float(be_trigger_tp)
    if not reached:
        return

    entry_price = float(trade.get("entry_price") or 0)
    if not entry_price:
        return
    cost_pts = compute_be_cost_pts(trade)
    be_price = round(entry_price + cost_pts, 2) if direction == "BUY" else round(entry_price - cost_pts, 2)

    mt5_ticket = trade.get("mt5_ticket")
    ticket_label = str(mt5_ticket) if mt5_ticket else trade["trade_id"][:8]
    trade_id = trade["trade_id"]
    last_alert_ts = last_alert.get(trade_id, 0.0)
    cooldown_active = (now - last_alert_ts) < _TP_SAFETY_NET_ALERT_COOLDOWN

    # The sweep only runs every 180s, so by the time it acts on a missed
    # TP touch, price can already have retraced past the point where a
    # breakeven+cost SL is even valid to set — e.g. for a BUY, be_price
    # must be below the current bid, or the broker rejects it outright
    # (retcode 10016, invalid stops) since that SL could never trigger as
    # a stop. Detect this up front instead of discovering it via a
    # doomed modify_order call every single sweep: confirmed live on
    # ticket 1543877939, which retried and re-alerted every 3 minutes
    # indefinitely because nothing gated re-attempts on the same trade.
    tick = await bridge.get_tick()
    if tick:
        window_closed = (
            (direction == "BUY" and be_price >= tick.bid) or
            (direction == "SELL" and be_price <= tick.ask)
        )
        if window_closed:
            if not cooldown_active:
                last_alert[trade_id] = now
                log.info(
                    "[TP-SafetyNet] ticket=%s %s territory was reached (extreme=%.2f) "
                    "but price has since retraced past the breakeven+cost level "
                    "(%.2f) — the protection window has closed, not retrying",
                    ticket_label, be_tp_label, extreme, be_price,
                )
                asyncio.create_task(telegram_alerts.send_message(
                    f"*TP Safety Net window closed*\n"
                    f"Ticket {ticket_label} ({trade.get('strategy', '?')}) reached "
                    f"{be_tp_label} territory (extreme {extreme:.2f}) but price has "
                    f"since retraced past the breakeven+cost level ({be_price:.2f}) — "
                    f"a stop there is no longer valid, so no SL move was attempted. "
                    f"This trade is running on its original SL/risk.",
                    trade_id, "tp_safety_net_window_closed",
                ))
            return

    if mt5_ticket:
        try:
            mt5_res = await bridge.modify_order(int(mt5_ticket), sl=be_price, tp=None)
        except Exception as e:
            log.warning("[TP-SafetyNet] ticket=%s modify_order raised: %s", ticket_label, e)
            return  # don't mark protected if the broker-side move failed
        # modify_order() reports broker-side rejection (invalid stops, position
        # already closed, requote, etc.) via a returned {"error": ...} dict, NOT
        # an exception — the try/except above alone never caught this, so a
        # rejected SL move was previously recorded as protected anyway. Confirmed
        # live: ticket 1543412796 was marked sl_moved_to_be=1 at 4157.08 here, but
        # MT5's own deal history showed the position was still closed by its
        # original, unmoved stop at 4150.00 for a real loss — exactly the outcome
        # this safety net exists to prevent.
        if not mt5_res.get("success"):
            log.warning(
                "[TP-SafetyNet] ticket=%s modify_order rejected by broker: %s "
                "— SL was NOT actually moved, trade remains at original risk",
                ticket_label, mt5_res.get("error", mt5_res),
            )
            if not cooldown_active:
                last_alert[trade_id] = now
                asyncio.create_task(telegram_alerts.send_message(
                    f"*TP Safety Net FAILED*\n"
                    f"Ticket {ticket_label} ({trade.get('strategy', '?')}) reached "
                    f"{be_tp_label} territory (extreme {extreme:.2f}) but the broker rejected "
                    f"the breakeven+cost SL move ({be_price:.2f}): "
                    f"{mt5_res.get('error', 'unknown error')}. SL was NOT moved — "
                    f"this trade remains at its original risk.",
                    trade_id, "tp_safety_net_failed",
                ))
            return  # don't mark protected if the broker-side move failed

    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?, sl_moved_to_be=1 WHERE trade_id=?",
            (be_price, trade["trade_id"]),
        )
    log.warning(
        "[TP-SafetyNet] ticket=%s reached %s (extreme=%.2f) but was never protected by "
        "the live loop — SL moved to breakeven+cost %.2f",
        ticket_label, be_tp_label, extreme, be_price,
    )
    asyncio.create_task(telegram_alerts.send_message(
        f"*TP Safety Net triggered*\n"
        f"Ticket {ticket_label} ({trade.get('strategy', '?')}) reached "
        f"{be_tp_label} territory (extreme {extreme:.2f}) but the live monitor never "
        f"registered it — SL has been moved to breakeven+cost ({be_price:.2f}) "
        f"to stop it giving back a favourable move as a full loss.",
        trade["trade_id"], "tp_safety_net",
    ))


def compute_be_cost_pts(trade: dict) -> float:
    """Round-trip cost estimate (spread+commission+slippage) in points,
    for a breakeven-plus-cost stop rather than a raw-entry stop that
    quietly bleeds the spread on every safety-net-protected trade."""
    try:
        fs = db_module.get_fee_settings()
        slippage_pts = float(fs.get("estimated_slippage_points", 5.0) or 5.0) * 0.01
        commission_rt = (
            float(fs.get("commission_per_lot_per_side", 0.0) or 0.0) * 2
            + float(fs.get("commission_round_turn_per_lot", 0.0) or 0.0)
        )
        return round(0.30 + slippage_pts + commission_rt / 100.0, 4)  # 0.30 = typical spread estimate
    except Exception:
        return 0.35
