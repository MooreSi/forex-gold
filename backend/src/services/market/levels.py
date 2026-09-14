"""Higher-timeframe bias and the key levels price reacts to.

Moved out of the Bounce engine when that engine was removed (2026-09-14).
Pure analysis: candles in, numbers and level dicts out. No database, no
parameters, no orders.

It was never Bounce-specific — the **Breakout** engine imports
`compute_htf_bias`, `identify_key_levels` and `is_news_window` from here and
always did, while they lived in another engine's package.

Everything that needed the Bounce engine's adaptive parameters -- its entry
trigger and the candle-pattern helpers only that used -- went with the engine.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from backend.src.services.dpm.engine import compute_atr  # noqa: F401  (re-exported)


def _ema(values: list[float], period: int) -> float:
    """Exponential Moving Average of the last `period` values."""
    if not values:
        return 0.0
    k = 2 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


def compute_htf_bias(h1_candles: list[dict]) -> str:
    """
    Returns 'bullish', 'bearish', or 'neutral' based on:
      - EMA20 vs EMA50 on H1 (with full warmup — all available closes fed in)
      - EMA20 slope vs 5 bars ago (trend must still be moving in the same direction)
      - Recent swing structure (higher highs/lows or lower highs/lows)
    """
    if len(h1_candles) < 52:
        return "neutral"

    closes = [float(c["close"]) for c in h1_candles if c.get("close")]
    if len(closes) < 52:
        return "neutral"

    # Use all available data for proper EMA warmup (not just the last N bars)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    price = closes[-1]

    # EMA slope: compare current EMA20 with 5 bars ago to confirm trend is fresh
    ema20_prev = _ema(closes[:-5], 20) if len(closes) > 57 else ema20
    ema20_rising = ema20 > ema20_prev
    ema20_falling = ema20 < ema20_prev

    # EMA signal: position + slope must agree
    if price > ema20 > ema50 and ema20_rising:
        ema_signal = "bullish"
    elif price < ema20 < ema50 and ema20_falling:
        ema_signal = "bearish"
    elif price > ema20 > ema50:
        ema_signal = "bullish"   # position counts even if slope is flat
    elif price < ema20 < ema50:
        ema_signal = "bearish"
    else:
        ema_signal = "neutral"

    # Swing structure: compare last 3 swings
    highs = [float(c["high"]) for c in h1_candles[-20:] if c.get("high")]
    lows  = [float(c["low"])  for c in h1_candles[-20:] if c.get("low")]
    if len(highs) >= 4 and len(lows) >= 4:
        hh = highs[-1] > highs[-3] and highs[-2] > highs[-4]
        hl = lows[-1]  > lows[-3]  and lows[-2]  > lows[-4]
        lh = highs[-1] < highs[-3] and highs[-2] < highs[-4]
        ll = lows[-1]  < lows[-3]  and lows[-2]  < lows[-4]
        if hh and hl:
            swing_signal = "bullish"
        elif lh and ll:
            swing_signal = "bearish"
        else:
            swing_signal = "neutral"
    else:
        swing_signal = "neutral"

    # Agree = confident, disagree = neutral
    if ema_signal == swing_signal and ema_signal != "neutral":
        return ema_signal
    if ema_signal != "neutral":
        return ema_signal
    return swing_signal


# ── Key level identification ──────────────────────────────────────────────────

def _swing_pivots(candles: list[dict], lookback: int = 3) -> list[dict]:
    """Identify swing high and low pivots over `lookback` bars either side."""
    levels: list[dict] = []
    for i in range(lookback, len(candles) - lookback):
        h = float(candles[i].get("high", 0) or 0)
        lo = float(candles[i].get("low", 0) or 0)
        if not h or not lo:
            continue
        surrounding_h = [float(candles[j].get("high", 0) or 0)
                         for j in range(i - lookback, i + lookback + 1) if j != i]
        surrounding_l = [float(candles[j].get("low", 0) or 0)
                         for j in range(i - lookback, i + lookback + 1) if j != i]
        if surrounding_h and h >= max(surrounding_h):
            levels.append({"price": h, "type": "resistance", "strength": 2})
        if surrounding_l and lo <= min(surrounding_l):
            levels.append({"price": lo, "type": "support", "strength": 2})
    return levels


def _round_number_levels(current_price: float, window: float = 60.0) -> list[dict]:
    """$10 round-number levels within `window` points of current price."""
    levels = []
    base = math.floor(current_price / 10) * 10
    for offset in range(-6, 7):
        price = base + offset * 10
        if abs(price - current_price) <= window:
            levels.append({"price": float(price), "type": "round", "strength": 1})
    return levels


def _session_range(h1_candles: list[dict]) -> list[dict]:
    """Asian session high/low from today's candles."""
    levels = []
    now_h = datetime.now(timezone.utc).hour
    # Look at candles from the last 24 bars to capture the Asian session range
    asian_candles = []
    for c in h1_candles[-24:]:
        ts = c.get("ts")
        if ts:
            try:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                h = dt.hour
                if h >= 23 or h < 8:
                    asian_candles.append(c)
            except Exception:
                pass
    if asian_candles:
        hi  = max(float(c.get("high", 0) or 0) for c in asian_candles)
        lo  = min(float(c.get("low",  0) or 0) for c in asian_candles)
        if hi > 0:
            levels.append({"price": hi, "type": "asian_high", "strength": 2})
        if lo > 0:
            levels.append({"price": lo, "type": "asian_low",  "strength": 2})
    return levels


def _daily_midpoint(h1_candles: list[dict]) -> Optional[dict]:
    """50% midpoint of today's daily range."""
    today_h  = max((float(c.get("high", 0) or 0) for c in h1_candles[-24:]), default=0)
    today_lo = min((float(c.get("low",  0) or 0) for c in h1_candles[-24:] if c.get("low")), default=0)
    if today_h > 0 and today_lo > 0:
        mid = round((today_h + today_lo) / 2, 2)
        return {"price": mid, "type": "daily_mid", "strength": 2}
    return None


def _fair_value_gaps(candles: list[dict], min_gap: float = 2.0) -> list[dict]:
    """
    Bullish FVG: candle[i].low > candle[i-2].high — price moved up too fast leaving a gap below.
    Bearish FVG: candle[i].high < candle[i-2].low — price moved down too fast leaving a gap above.
    The FVG midpoint is a high-probability area for price to retrace to and bounce from.
    Strength=3 — FVGs are institutional imbalance zones and highly reliable on gold.
    """
    levels = []
    for i in range(2, len(candles)):
        c0_hi = float(candles[i - 2].get("high", 0) or 0)
        c0_lo = float(candles[i - 2].get("low",  0) or 0)
        c2_hi = float(candles[i].get("high", 0) or 0)
        c2_lo = float(candles[i].get("low",  0) or 0)
        if not (c0_hi and c0_lo and c2_hi and c2_lo):
            continue
        # Bullish FVG — gap above prior candle high (price gapped up)
        if c2_lo > c0_hi + min_gap:
            mid = round((c0_hi + c2_lo) / 2, 2)
            levels.append({"price": mid, "type": "fvg_bull", "strength": 3})
        # Bearish FVG — gap below prior candle low (price gapped down)
        if c2_hi < c0_lo - min_gap:
            mid = round((c0_lo + c2_hi) / 2, 2)
            levels.append({"price": mid, "type": "fvg_bear", "strength": 3})
    return levels[-12:]  # keep most recent 12 FVGs


def _order_blocks(candles: list[dict]) -> list[dict]:
    """
    Bullish OB: the last bearish candle before a strong 3-candle bullish impulse.
    Bearish OB: the last bullish candle before a strong 3-candle bearish impulse.
    Order blocks mark where institutions placed large orders — price regularly returns to them.
    Strength=3 — OBs coinciding with swing levels get elevated to 4 by deduplication.
    """
    levels = []
    n = len(candles)
    for i in range(n - 4):
        c = candles[i]
        op = float(c.get("open",  0) or 0)
        cl = float(c.get("close", 0) or 0)
        hi = float(c.get("high",  0) or 0)
        lo = float(c.get("low",   0) or 0)
        if not (op and cl and hi and lo):
            continue

        next3 = candles[i + 1: i + 4]
        bull_run = sum(
            1 for cc in next3
            if float(cc.get("close", 0) or 0) > float(cc.get("open", 0) or 0)
        )
        bear_run = sum(
            1 for cc in next3
            if float(cc.get("close", 0) or 0) < float(cc.get("open", 0) or 0)
        )

        if bull_run >= 2 and cl < op:
            # Bearish candle before bullish impulse → bullish OB at its body low–open
            ob_price = round((lo + op) / 2, 2)
            levels.append({"price": ob_price, "type": "ob_bull", "strength": 3})

        if bear_run >= 2 and cl > op:
            # Bullish candle before bearish impulse → bearish OB at its body close–high
            ob_price = round((cl + hi) / 2, 2)
            levels.append({"price": ob_price, "type": "ob_bear", "strength": 3})

    return levels[-10:]


def _liquidity_pools(h1_candles: list[dict]) -> list[dict]:
    """
    Clusters of swing highs / lows within 1 point of each other become liquidity pools —
    areas where retail stop-losses are stacked.  Institutions sweep these zones before reversing.
    Strength=2; rises to 3+ when a sweep is detected (handled in check_entry_trigger).
    """
    highs = []
    lows  = []
    for c in h1_candles[-40:]:
        h = float(c.get("high", 0) or 0)
        l = float(c.get("low",  0) or 0)
        if h:
            highs.append(h)
        if l:
            lows.append(l)

    levels: list[dict] = []
    # Group highs that are within 1 point of each other
    for h in highs:
        cluster = [x for x in highs if abs(x - h) <= 1.0]
        if len(cluster) >= 2:
            levels.append({"price": round(sum(cluster) / len(cluster), 2),
                           "type": "liq_high", "strength": 2})
    for l in lows:
        cluster = [x for x in lows if abs(x - l) <= 1.0]
        if len(cluster) >= 2:
            levels.append({"price": round(sum(cluster) / len(cluster), 2),
                           "type": "liq_low", "strength": 2})
    return levels


def identify_key_levels(
    h1_candles: list[dict],
    current_price: float,
    m15_candles: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Aggregate key levels from: swing pivots, round numbers, session range, daily midpoint,
    Fair Value Gaps (institutional imbalance), Order Blocks, and Liquidity Pools.
    De-duplicate levels within 2 points of each other (keep highest strength).
    Returns list sorted by distance from current price.
    """
    levels: list[dict] = []
    levels += _swing_pivots(h1_candles)
    levels += _round_number_levels(current_price)
    levels += _session_range(h1_candles)
    levels += _order_blocks(h1_candles)
    levels += _liquidity_pools(h1_candles)
    mid = _daily_midpoint(h1_candles)
    if mid:
        levels.append(mid)
    # FVGs on M15 are more precise for scalp entries
    if m15_candles and len(m15_candles) >= 10:
        levels += _fair_value_gaps(m15_candles, min_gap=1.5)

    # De-duplicate: cluster levels within 2.5 points
    merged: list[dict] = []
    for lv in sorted(levels, key=lambda x: -x["strength"]):
        close = next((m for m in merged if abs(m["price"] - lv["price"]) < 2.5), None)
        if close:
            close["strength"] += 1
        else:
            merged.append(dict(lv))

    # Filter to levels within 150 points of current price
    merged = [m for m in merged if abs(m["price"] - current_price) <= 150]
    merged.sort(key=lambda x: abs(x["price"] - current_price))
    return merged


# ── News suppression window ───────────────────────────────────────────────────

# Known FOMC rate decision days (UTC date of the Wednesday announcement).
# Rate decision at ~19:00 UTC, press conference ~19:30 UTC.
# Suppress 12:00–22:00 UTC on these dates.
_FOMC_DATES_2026: set[tuple[int, int]] = {
    (1, 29), (3, 19), (5, 7), (6, 18),   # Jan–Jun already passed
    (7, 29), (9, 17), (11, 5), (12, 16),  # Jul–Dec upcoming
}


def is_news_window() -> bool:
    """
    Delegates to news_filter.is_high_impact_window() which uses the live
    Forex Factory calendar feed, falling back to hardcoded dates on failure.
    """
    try:
        from backend.src.services.market import news_window as _nf
        return _nf.is_high_impact_window()
    except Exception:
        # Last-resort fallback: hardcoded routine-data suppression only
        now = datetime.now(timezone.utc)
        return now.hour in (7, 8, 13, 14, 15, 16) and now.minute < 5


# ── Entry trigger ─────────────────────────────────────────────────────────────
