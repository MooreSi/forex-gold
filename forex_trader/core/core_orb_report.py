"""ORB/IVB report + auto-execute -- rebuilt 2026-08-01 to the classic
Opening-Range-Breakout methodology (see
https://www.litefinance.org/blog/for-beginners/trading-strategies/opening-range-breakout-strategy/)
applied to this account's session structure:

- Reference range: the WHOLE Asian session (00:00-08:00 UTC) -- restores
  the pre-2026-07-22 full-session range this report used before a run of
  rewrites narrowed it to just the hour before London open (see
  FOREX-OLD's forex_trader/core/engine.py for that earlier version). Used
  here purely as a confirmation FILTER, not the traded range itself.
- Opening range: the first 15 minutes of the London session (08:00-08:15
  UTC) -- the actual "opening range" the article defines, and the range
  whose breakout is traded.
- Direction/entry: only confirmed when price clears the London opening
  range AND the Asian range in the SAME direction. A breakout of the
  (narrow) opening range that's still sitting inside the (wide) Asian
  range is reported as "unconfirmed", not a trade signal -- it isn't
  actually a continuation past the wider overnight structure yet.
- Stop: at the midpoint of the London opening range (the article's
  "classic method"), which for a boundary breakout is algebraically the
  same as "0.5x opening-range height beyond the breakout edge".
- Target: 2x the resulting risk (the article's first partial-TP ratio,
  2:1) -- auto-executed. A second, informational-only 3:1 level is also
  reported (the article's second partial) but NOT auto-executed: the EA's
  orb_fixed management (ManageOrbFixed in ForexTraderBridge.mq5) is a
  single-TP close-all, not a partial-close ladder, and wiring a real
  ladder would need an EA-side change this rebuild doesn't make.

The earlier rewrites' volume-profile POC/VAH/VAL and "reload zone"
pullback-entry concept are REMOVED -- the article enters on the breakout
itself once confirmed, not on a pullback into a value-area pocket, and a
volume profile computed over a 15-candle opening range was a poor fit for
that mechanism anyway. Auto-execute now places a genuine immediate MARKET
order (core_manual_market_order.open_manual_market_order, the same call
the manual Execute button already used) rather than a resting
pending-limit order at a reload zone that no longer exists.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.models import STRATEGY_ORB_FIXED

log = logging.getLogger(__name__)

_ASIA_START_HOUR   = 0
_ASIA_END_HOUR     = 8    # 00:00-08:00 UTC -- confirmation-filter range
_LONDON_OR_MINUTES = 15   # 08:00-08:15 UTC -- the traded opening range

_ORB_SL_RANGE_PCT   = 0.50  # stop = breakout edge -/+ this fraction of the OR height (== OR midpoint)
_ORB_TARGET_R_MULT  = 2.0   # TP1 (auto-executed): 2:1 reward:risk, per the article
_ORB_TARGET2_R_MULT = 3.0   # TP2 (informational only, not auto-executed): 3:1


async def build_orb_report(bridge: Any) -> Optional[dict]:
    """
    Returns None before London opens today, or if candle data isn't
    available. Returns a "forming" report (no direction/stop/target yet)
    during the 08:00-08:15 UTC opening-range window itself. From 08:15
    onward, evaluates the confirmed breakout state for the rest of the
    day -- unlike the earlier rewrites, this isn't bounded to a further
    narrow window after London opens, since a genuine breakout can arrive
    at any point once the opening range is established.
    """
    from zoneinfo import ZoneInfo

    tick = await bridge.get_tick()
    if tick is None:
        return None

    now_utc = datetime.now(timezone.utc)
    london_now = now_utc.astimezone(ZoneInfo("Europe/London"))
    london_open_local = london_now.replace(hour=8, minute=0, second=0, microsecond=0)
    london_open_utc = london_open_local.astimezone(timezone.utc)
    or_start = london_open_utc.timestamp()
    or_end = or_start + _LONDON_OR_MINUTES * 60
    now_ts = now_utc.timestamp()

    if now_ts < or_start:
        return None  # London hasn't opened yet today

    asia_start = london_open_utc.replace(
        hour=_ASIA_START_HOUR, minute=0, second=0, microsecond=0
    ).timestamp()
    asia_end = london_open_utc.replace(
        hour=_ASIA_END_HOUR, minute=0, second=0, microsecond=0
    ).timestamp()
    asia_candles = await bridge.get_candles_range(asia_start, asia_end, timeframe="M1")
    if not asia_candles:
        return None
    asia_high = max(c["high"] for c in asia_candles)
    asia_low = min(c["low"] for c in asia_candles)
    asia_range = asia_high - asia_low

    current_price = float(tick.ask)

    if now_ts < or_end:
        return {
            "current_price": current_price,
            "phase": "forming",
            "asia_high": asia_high, "asia_low": asia_low, "asia_range": asia_range,
            "or_start": or_start, "or_end": or_end,
            "direction": "inside",
            "position_note": (
                "London opening range still forming — closes at "
                f"{datetime.fromtimestamp(or_end, tz=timezone.utc).strftime('%H:%M')} UTC"
            ),
        }

    or_candles = await bridge.get_candles_range(or_start, or_end, timeframe="M1")
    if not or_candles:
        return None
    or_high = max(c["high"] for c in or_candles)
    or_low = min(c["low"] for c in or_candles)
    or_range = or_high - or_low
    if or_range <= 0:
        return None

    # Candles for the chart / breakout context: opening range through now.
    post_or_candles = await bridge.get_candles_range(or_start, now_ts + 60, timeframe="M1")
    all_candles = post_or_candles or or_candles

    broke_up = current_price > or_high
    broke_down = current_price < or_low
    if broke_up and current_price > asia_high:
        direction = "bullish"
    elif broke_down and current_price < asia_low:
        direction = "bearish"
    elif broke_up or broke_down:
        direction = "unconfirmed"  # cleared the opening range but still inside the Asian range
    else:
        direction = "inside"

    report: dict = {
        "current_price": current_price,
        "phase": "active",
        "asia_high": asia_high, "asia_low": asia_low, "asia_range": asia_range,
        "or_high": or_high, "or_low": or_low, "or_range": or_range,
        "or_start": or_start, "or_end": or_end,
        "direction": direction,
        "candles": all_candles,
        # Kept for the ORB/IVB Report tab's existing "range_*" field names --
        # now means the London opening range (the traded range), not the old
        # pre-London reference range.
        "range_low": or_low, "range_high": or_high, "range_height": or_range,
    }

    if direction in ("inside", "unconfirmed"):
        if direction == "unconfirmed":
            note = (
                "broke the London opening range but still inside the Asian range "
                f"(${asia_low:.2f}–${asia_high:.2f}) — not confirmed"
            )
        else:
            note = (
                f"still inside the London opening range — {current_price - or_low:.1f} pts "
                f"above the Low, {or_high - current_price:.1f} pts below the High"
            )
        report.update({
            "stop": None, "target": None, "target2": None, "rr": None,
            "position_note": note,
        })
        return report

    breakout_edge = or_high if direction == "bullish" else or_low
    stop = (
        breakout_edge - _ORB_SL_RANGE_PCT * or_range if direction == "bullish"
        else breakout_edge + _ORB_SL_RANGE_PCT * or_range
    )
    risk = abs(breakout_edge - stop)
    target = (
        breakout_edge + _ORB_TARGET_R_MULT * risk if direction == "bullish"
        else breakout_edge - _ORB_TARGET_R_MULT * risk
    )
    target2 = (
        breakout_edge + _ORB_TARGET2_R_MULT * risk if direction == "bullish"
        else breakout_edge - _ORB_TARGET2_R_MULT * risk
    )
    reward = abs(target - breakout_edge)
    rr = round(reward / risk, 2) if risk > 0 else None

    report.update({
        "stop": stop, "target": target, "target2": target2, "rr": rr,
        "risk": risk, "reward": reward,
        "sl_range_pct": _ORB_SL_RANGE_PCT,
        "target_r_mult": _ORB_TARGET_R_MULT, "target2_r_mult": _ORB_TARGET2_R_MULT,
        "position_note": (
            f"{direction} breakout of the London opening range, confirmed beyond the "
            f"Asian range — {abs(current_price - breakout_edge):.1f} pts past the "
            f"{'High' if direction == 'bullish' else 'Low'}"
        ),
    })
    return report


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
        from forex_trader.core.core_close_trade import get_trading_balance
        from forex_trader.core.core_fees_sizing import suggest_lot_size
        _entry_approx = float(report.get("current_price") or 0) or stop_loss
        _risk_pct = float(rs.get("risk_per_trade_pct", 0.5))
        _balance = await get_trading_balance(bridge, 1000.0)
        lot = suggest_lot_size(_entry_approx, stop_loss, _balance, _risk_pct)

    from forex_trader.core.core_manual_market_order import open_manual_market_order
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
