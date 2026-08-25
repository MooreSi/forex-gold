"""ORB/IVB (Opening-Range Breakout) report -- the read-only half.

Rebuilt upstream on 2026-08-01 to the classic Opening-Range-Breakout
methodology; the full rationale is kept verbatim below. The other half,
orb_auto_execute(), places a genuine order and therefore lives in
backend/src/services/trading/orb_execute.py -- the split is safe because the
two halves share nothing: auto-execute takes the finished report as an
argument and never calls back into this module.

ORB/IVB report + auto-execute -- rebuilt 2026-08-01 to the classic
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

from backend.src.db import database as db_module
from backend.src.services.telegram import alerts
from backend.src.utils.models import STRATEGY_ORB_FIXED

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
