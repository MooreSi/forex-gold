"""New York opening-range breakout, traded with the H4 trend (2026-10-01).

The London ORB (`orb_report.py`) was measured before this was written, by
replaying it and its variants over the bridge's own gold candles (M5,
Oct 2025 - Sep 2026; M15, Dec 2023 - Sep 2026). Three things were wrong
with it, and each is a reason this module exists rather than a patch:

1. **London is the wrong open for a gold breakout.** Every London variant
   (15/30/60-minute range, stop at the midpoint or the far side, 1-3R) lost
   or broke even; the 60-minute range with a midpoint stop lost 0.16-0.24R a
   trade. The New York cash open (09:30 ET) is where gold's breakouts carry.
2. **The live report could never take its own trade.** Direction was only
   "confirmed" once price cleared the whole Asian range, but the target sat
   one opening-range height beyond the 15-minute range's edge. By the time
   the Asian range broke, price was usually past the target, so the day was
   skipped as "stale" -- 7 of the 12 September days it fired. The trades
   that did fill carried the target from the edge but entered at the market:
   2026-09-11 bought 4362.35 with the stop 9.34 away and the target 0.12
   away.
3. **Counter-trend breakouts are where the losses were.** Splitting every
   variant by the H4 close against its EMA50, breakouts WITH that trend won
   and breakouts against it lost, in every year and on both data sets.

The rules, as replayed (M5 +0.27R a trade, 100 trades in 11 months; M15
+0.14R, 266 trades in 2.8 years; held to stop or target):

- Range: the first 30 minutes after 09:30 New York (DST handled by zoneinfo).
- Trigger: the FIRST M5 bar to close beyond the range inside the next 90
  minutes. It decides the day -- a second breakout is not looked for.
- Trend: that close must be on the H4 EMA50's side of the break (buy above,
  sell below). Against it, the day is skipped.
- No chasing: a close more than a quarter of the range beyond the edge
  skips the day, and so does a price that has since run that far.
- Stop at the FAR side of the range; target 2R from the price actually paid.

Read-only: candles and a tick in, a report out. `trading/orb_execute`
places the order, as it does for the London report.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
OPEN_HOUR, OPEN_MINUTE = 9, 30
OR_MINUTES = 30
ENTRY_WINDOW_MINUTES = 90
CHASE_FRACTION = 0.25     # of the range, beyond the broken edge
TARGET_R = 2.0
TREND_EMA = 50
BAR_S = 300               # M5
# How long after the trigger bar closed an entry may still be taken. The
# scheduler looks once a minute; a trigger older than this was missed (a
# restart, a dropped bridge) and is not chased.
FRESH_S = 10 * 60
H4_BARS = 120


def open_utc(now_utc: datetime) -> float:
    """09:30 New York on `now_utc`'s New York date, as a UTC timestamp."""
    ny = now_utc.astimezone(NY)
    return ny.replace(hour=OPEN_HOUR, minute=OPEN_MINUTE, second=0,
                      microsecond=0).timestamp()


def ema(values: list, period: int) -> Optional[float]:
    """SMA-seeded EMA of `values`; None without `period` of them."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def trend(h4_closes: list) -> Optional[str]:
    """"up" when the last closed H4 bar closed above its EMA50, "down" below,
    None when there is not enough history to say."""
    e = ema(h4_closes, TREND_EMA)
    if e is None:
        return None
    return "up" if h4_closes[-1] > e else "down"


def evaluate(m5: list, h4_closes: list, now_ts: float, bid: float,
             ask: float) -> dict:
    """The report for this moment. `m5` is true-UTC M5 bars from the range
    start onward (a forming bar is ignored); `h4_closes` are CLOSED H4 bars.

    `phase` is one of: before, forming, watching, signal, done. `done` means
    the day is decided -- a skipped breakout, a missed window -- and carries
    the reason in `position_note`. Only `signal` carries a direction.
    """
    now_utc = datetime.fromtimestamp(now_ts, timezone.utc)
    or_start = open_utc(now_utc)
    or_end = or_start + OR_MINUTES * 60
    entry_end = or_end + ENTRY_WINDOW_MINUTES * 60
    mid = (bid + ask) / 2.0
    base = {"session": "new_york", "or_start": or_start, "or_end": or_end,
            "entry_end": entry_end, "current_price": mid,
            "direction": "inside", "stop": None, "target": None}
    if now_utc.astimezone(NY).weekday() >= 5 or now_ts < or_start:
        return {**base, "phase": "before", "position_note": "New York has not opened"}

    closed = [b for b in m5 if float(b["ts"]) + BAR_S <= now_ts]
    in_range = [b for b in closed if or_start <= float(b["ts"]) < or_end]
    if now_ts < or_end:
        return {**base, "phase": "forming",
                "position_note": "New York opening range still forming"}
    if len(in_range) < (OR_MINUTES * 60 // BAR_S) // 2:
        return {**base, "phase": "done",
                "position_note": f"only {len(in_range)} M5 bars in the opening range"}
    hi = max(float(b["high"]) for b in in_range)
    lo = min(float(b["low"]) for b in in_range)
    width = hi - lo
    base.update(or_high=hi, or_low=lo, or_range=width,
                range_high=hi, range_low=lo, range_height=width)
    if width <= 0:
        return {**base, "phase": "done", "position_note": "a flat opening range"}

    trigger = None
    for b in closed:
        t = float(b["ts"])
        if t < or_end or t + BAR_S > entry_end:
            continue
        c = float(b["close"])
        if c > hi or c < lo:
            trigger = b
            break
    if trigger is None:
        if now_ts >= entry_end:
            return {**base, "phase": "done",
                    "position_note": "no M5 close beyond the range in the entry window"}
        return {**base, "phase": "watching",
                "position_note": f"range {lo:.2f}-{hi:.2f}, waiting for an M5 close beyond it"}

    c = float(trigger["close"])
    up = c > hi
    edge = hi if up else lo
    side = "bullish" if up else "bearish"
    base["trigger_ts"] = float(trigger["ts"])
    t = trend(h4_closes)
    base["trend"] = t
    if t is None:
        return {**base, "phase": "done", "position_note": "not enough H4 history for the trend"}
    if (t == "up") != up:
        return {**base, "phase": "done",
                "position_note": f"{side} breakout against the H4 {t}trend -- skipped"}
    if abs(c - edge) > CHASE_FRACTION * width:
        return {**base, "phase": "done",
                "position_note": f"breakout bar closed {abs(c - edge):.2f} past the edge "
                                 f"(more than {CHASE_FRACTION:g} of the range) -- chasing"}
    if now_ts - (float(trigger["ts"]) + BAR_S) > FRESH_S:
        return {**base, "phase": "done", "position_note": "the breakout was missed; not chasing it"}

    entry = ask if up else bid
    stop = lo if up else hi
    if (up and (entry <= stop or entry - edge > CHASE_FRACTION * width)) or \
            (not up and (entry >= stop or edge - entry > CHASE_FRACTION * width)):
        return {**base, "phase": "done",
                "position_note": f"price {entry:.2f} is no longer near the {side} edge -- skipped"}
    risk = abs(entry - stop)
    target = entry + TARGET_R * risk if up else entry - TARGET_R * risk
    return {**base, "phase": "signal", "direction": side, "current_price": entry,
            "stop": stop, "target": target, "risk": risk,
            "reward": TARGET_R * risk, "rr": TARGET_R,
            "position_note": f"{side} New York breakout with the H4 trend; "
                             f"stop {stop:.2f}, target {target:.2f}"}


async def build_report(bridge: Any, now_ts: Optional[float] = None) -> Optional[dict]:
    """Fetch what `evaluate` needs from the bridge. None without a tick."""
    import time as _time
    now_ts = _time.time() if now_ts is None else now_ts
    tick = await bridge.get_tick()
    if tick is None:
        return None
    start = open_utc(datetime.fromtimestamp(now_ts, timezone.utc))
    m5 = await bridge.get_candles_range(start, now_ts + 60, timeframe="M5") or []
    # The bridge converts to UTC with an offset measured off the last tick,
    # unrounded, so in a quiet market the stamps come back seconds late.
    m5 = [{**b, "ts": round(float(b["ts"]) / 60.0) * 60} for b in m5]
    h4 = await bridge.get_candles("H4", H4_BARS) or []
    # get_candles' newest H4 bar is still forming.
    closes = [float(b["close"]) for b in h4[:-1]]
    return evaluate(m5, closes, now_ts, float(tick.bid), float(tick.ask))
