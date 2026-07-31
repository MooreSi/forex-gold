"""
Rules-based XAUUSD signal generator.

Replicates and improves on the Gold Diggers methodology:
  1. H1 HTF bias — EMA20/50 + swing structure
  2. Key level identification — swing pivots, round numbers, session range
  3. M15 entry trigger — price tests level + candle close confirmation
  4. ATR-based SL/TP — minimum 1:1 R:R enforced on TP1

No MT5 orders are placed. This module only analyses data.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from forex_trader.core.dpm_engine import compute_atr
from backend.src.services.test_signal import adaptive_params as ap


# ── Session detection ─────────────────────────────────────────────────────────

_SESSIONS = {
    "asian":   (23, 8),   # 23:00–08:00 UTC (wraps midnight)
    "london":  (7, 12),
    "overlap": (12, 16),
    "ny":      (12, 17),
    "off":     (17, 23),
}


def get_session() -> str:
    now = datetime.now(timezone.utc)
    dow = now.weekday()  # 0=Mon … 6=Sun
    h   = now.hour
    # XAUUSD is closed Saturday all day and Sunday until ~22:00 UTC
    if dow == 5:           # Saturday — fully closed
        return "closed"
    if dow == 6 and h < 22:  # Sunday before Asian open
        return "closed"
    if 23 <= h or h < 8:
        return "asian"
    if 8 <= h < 12:
        return "london"
    if 12 <= h < 17:
        return "overlap"   # London/NY overlap 12-14, then pure NY 14-17
    if 17 <= h < 21:
        return "ny"
    return "off"


def session_quality(session: str) -> str:
    asian_allowed = ap.get("allow_asian") >= 0.5
    return {
        "overlap": "high",
        "london":  "good",
        "ny":      "good",
        "asian":   "good" if asian_allowed else "low",
        "off":     "low",
        "closed":  "closed",  # weekend / market-closed — never trade
    }.get(session, "low")


def session_is_active(session: str) -> bool:
    return session_quality(session) in ("high", "good")


# ── HTF bias ──────────────────────────────────────────────────────────────────

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
        from backend.src.services.test_signal import news_filter as _nf
        return _nf.is_high_impact_window()
    except Exception:
        # Last-resort fallback: hardcoded routine-data suppression only
        now = datetime.now(timezone.utc)
        return now.hour in (7, 8, 13, 14, 15, 16) and now.minute < 5


# ── Entry trigger ─────────────────────────────────────────────────────────────

def _candle_is_bullish(c: dict) -> bool:
    return float(c.get("close", 0) or 0) > float(c.get("open", 0) or 0)


def _candle_is_bearish(c: dict) -> bool:
    return float(c.get("close", 0) or 0) < float(c.get("open", 0) or 0)


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l < 1e-9:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _is_bullish_engulfing(last_c: dict, prev_c: dict) -> bool:
    """Last candle is bullish and its body fully engulfs the prior bearish candle's body."""
    if not _candle_is_bullish(last_c) or not _candle_is_bearish(prev_c):
        return False
    l_lo = min(float(last_c.get("open", 0) or 0), float(last_c.get("close", 0) or 0))
    l_hi = max(float(last_c.get("open", 0) or 0), float(last_c.get("close", 0) or 0))
    p_lo = min(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_hi = max(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    return l_lo <= p_lo and l_hi >= p_hi and (l_hi - l_lo) > (p_hi - p_lo) * 1.05


def _is_bearish_engulfing(last_c: dict, prev_c: dict) -> bool:
    if not _candle_is_bearish(last_c) or not _candle_is_bullish(prev_c):
        return False
    l_lo = min(float(last_c.get("open", 0) or 0), float(last_c.get("close", 0) or 0))
    l_hi = max(float(last_c.get("open", 0) or 0), float(last_c.get("close", 0) or 0))
    p_lo = min(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    p_hi = max(float(prev_c.get("open", 0) or 0), float(prev_c.get("close", 0) or 0))
    return l_lo <= p_lo and l_hi >= p_hi and (l_hi - l_lo) > (p_hi - p_lo) * 1.05


def _is_hammer(c: dict) -> bool:
    """Bullish pin bar: small body, lower wick >= 2× body, minimal upper wick."""
    op = float(c.get("open", 0) or 0)
    cl = float(c.get("close", 0) or 0)
    hi = float(c.get("high", 0) or 0)
    lo = float(c.get("low",  0) or 0)
    body       = abs(cl - op)
    lower_wick = min(op, cl) - lo
    upper_wick = hi - max(op, cl)
    rng        = hi - lo
    if rng < 1e-9:
        return False
    return lower_wick >= 2.0 * body and upper_wick <= body * 0.5 and body / rng < 0.35


def _is_shooting_star(c: dict) -> bool:
    """Bearish pin bar: small body, upper wick >= 2× body, minimal lower wick."""
    op = float(c.get("open", 0) or 0)
    cl = float(c.get("close", 0) or 0)
    hi = float(c.get("high", 0) or 0)
    lo = float(c.get("low",  0) or 0)
    body       = abs(cl - op)
    upper_wick = hi - max(op, cl)
    lower_wick = min(op, cl) - lo
    rng        = hi - lo
    if rng < 1e-9:
        return False
    return upper_wick >= 2.0 * body and lower_wick <= body * 0.5 and body / rng < 0.35


def _is_liquidity_sweep_bull(last_c: dict, prev_c: dict, level_price: float) -> bool:
    """
    Bullish sweep: prior candle wicked below the level (taking out stops) but closed above it,
    AND current candle is bullish (confirming reversal after the sweep).
    """
    prev_low   = float(prev_c.get("low",   0) or 0)
    prev_close = float(prev_c.get("close", 0) or 0)
    return prev_low < level_price and prev_close > level_price and _candle_is_bullish(last_c)


def _is_liquidity_sweep_bear(last_c: dict, prev_c: dict, level_price: float) -> bool:
    """
    Bearish sweep: prior candle wicked above the level (taking out stops) but closed below it,
    AND current candle is bearish (confirming reversal after the sweep).
    """
    prev_high  = float(prev_c.get("high",  0) or 0)
    prev_close = float(prev_c.get("close", 0) or 0)
    return prev_high > level_price and prev_close < level_price and _candle_is_bearish(last_c)


def _macd_fading_for_buy(m15_candles: list[dict]) -> bool:
    """
    True when selling momentum is exhausting (MACD histogram rising from recent lows
    OR not strongly negative) — valid condition for a BUY bounce at support.
    False when bearish momentum is ACCELERATING — wrong time to buy.
    """
    closes = [float(c.get("close", 0) or 0) for c in m15_candles]
    if len(closes) < 38:
        return True
    _, hist_now  = compute_macd_hist(closes)
    _, hist_prev = compute_macd_hist(closes[:-3])
    # Allow if MACD is rising (selling momentum decelerating) OR mild bearish
    return hist_now > hist_prev or hist_now > -2.0


def _macd_fading_for_sell(m15_candles: list[dict]) -> bool:
    """
    True when buying momentum is exhausting (MACD histogram falling from recent highs
    OR not strongly positive) — valid condition for a SELL bounce at resistance.
    False when bullish momentum is ACCELERATING — wrong time to sell.
    """
    closes = [float(c.get("close", 0) or 0) for c in m15_candles]
    if len(closes) < 38:
        return True
    _, hist_now  = compute_macd_hist(closes)
    _, hist_prev = compute_macd_hist(closes[:-3])
    # Allow if MACD is falling (buying momentum decelerating) OR mild bullish
    return hist_now < hist_prev or hist_now < 2.0


def _candle_body_atr_ratio(c: dict, atr: float) -> float:
    """Ratio of candle body size to ATR. <0.15 = doji / indecision."""
    if atr < 1e-6:
        return 1.0
    body = abs(float(c.get("close", 0) or 0) - float(c.get("open", 0) or 0))
    return body / atr


def check_entry_trigger(
    m15_candles: list[dict],
    h1_candles: list[dict],
    key_levels: list[dict],
    htf_bias: str,
    current_price: float,
    atr: float,
    *,
    h4_bias: str = "neutral",
    regime: str = "neutral",
    macd_hist: float = 0.0,
    session: str = "",
) -> Optional[dict]:
    """
    Multi-pattern entry detector (5 patterns):
      1. Standard bounce: 2 consecutive candles in direction at key level
      2. Engulfing: single large reversal candle at key level
      3. Pin bar: hammer / shooting star at key level
      4. RSI divergence at key level (weakened momentum before reversal)
      5. Liquidity sweep: prior candle wicked through level then closed back — institutional stop hunt

    FVG/OB/liquidity-pool levels from identify_key_levels() are now included in key_levels,
    so patterns fire at those zones automatically.

    Direction must align with HTF bias unless allow_counter_bias or session-aware logic permits it.
    Liquidity sweep is exempt from the counter-bias block — sweeps ARE the institutional move.
    """
    if len(m15_candles) < 4 or atr < 1.0:
        return None

    from backend.src.utils.regime import (
        BOUNCE_BLOCKED_ENTRY_LEVELS,
        bounce_pattern_allowed,
    )

    zone_width = max(atr * ap.get("zone_width_atr_mult"), 4.0)

    last_c  = m15_candles[-1]
    prev_c  = m15_candles[-2]

    last_low  = float(last_c.get("low",  0) or 0)
    last_high = float(last_c.get("high", 0) or 0)

    # Pattern × day-type permission (measured, see core/regime.py):
    # engulfing/pin_bar earn everywhere except strong trends; sweep only in
    # range/neutral; 2-candle bounce only in true ranges.
    _sweep_ok,  _ = bounce_pattern_allowed("liquidity_sweep", regime)
    _bounce_ok, _ = bounce_pattern_allowed("bounce", regime)
    _englf_ok,  _ = bounce_pattern_allowed("engulfing", regime)

    for level in key_levels:
        lp   = level["price"]
        dist = abs(current_price - lp)
        if dist > zone_width:
            continue

        lv_type   = level.get("type", "")
        lv_str    = level["strength"]

        # Entry-level blacklist: bounce entries AT liquidity pools and FVGs
        # lost -$1,685 combined (liq_low alone: 37% WR, -$744). These zones
        # break more often than they hold — they stay in key_levels for TP
        # targeting but are no longer valid bounce entry bases.
        if lv_type in BOUNCE_BLOCKED_ENTRY_LEVELS:
            continue

        # ── BUY setups at support ─────────────────────────────────────────────
        if lp < current_price:
            # Bearish structural zones (OB/FVG created on a down-move) must not
            # be used as buy support — they are SELL zones by design.
            if lv_type in ("ob_bear", "fvg_bear"):
                continue

            price_tested = last_low <= lp + zone_width * 0.5
            # For OB levels: price being inside the zone counts as a test
            if lv_type == "ob_bull":
                price_tested = price_tested or (current_price <= lp + atr * 0.6)
            if not price_tested:
                continue

            counter_ok = (ap.get("allow_counter_bias") >= 0.5
                          or _counter_bias_allowed(session, lv_type, regime))
            bias_ok = counter_ok or htf_bias not in ("bearish",)

            # Pattern 5: Liquidity sweep — exempt from bias block (it IS the
            # institutional move) but restricted to range/neutral days: 57% WR
            # overall yet -$683 lifetime, with every profitable sweep coming
            # outside trending days.
            if _sweep_ok and _is_liquidity_sweep_bull(last_c, prev_c, lp) and lv_str >= 2:
                return {
                    "direction": "BUY",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 2,
                    "price": current_price,
                    "zone_low":  round(lp - 0.5, 2),
                    "zone_high": round(lp + atr * 0.35, 2),
                    "trigger_pattern": "liquidity_sweep",
                }

            if not bias_ok:
                continue

            # Pattern 1: two bullish candles (standard bounce) — range days only
            # (48% WR / -$678 lifetime elsewhere) and structural strength >= 3.
            if (_bounce_ok
                    and _candle_is_bullish(last_c) and _candle_is_bullish(prev_c)
                    and lv_str >= 3
                    and _candle_body_atr_ratio(last_c, atr) >= 0.15
                    and _macd_fading_for_buy(m15_candles)):
                return {
                    "direction": "BUY",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str,
                    "price": current_price,
                    "zone_low":  round(lp - 1.0, 2),
                    "zone_high": round(lp + atr * 0.4, 2),
                    "trigger_pattern": "bounce",
                }

            # Pattern 2: bullish engulfing at support (75% WR, +$466 lifetime)
            if _englf_ok and _is_bullish_engulfing(last_c, prev_c):
                return {
                    "direction": "BUY",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 1,
                    "price": current_price,
                    "zone_low":  round(lp - 1.0, 2),
                    "zone_high": round(lp + atr * 0.4, 2),
                    "trigger_pattern": "engulfing",
                }

            # Pattern 3: hammer pin bar at support (75% WR, +$216 lifetime)
            if _englf_ok and _is_hammer(last_c):
                return {
                    "direction": "BUY",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 1,
                    "price": current_price,
                    "zone_low":  round(lp - 1.0, 2),
                    "zone_high": round(lp + atr * 0.4, 2),
                    "trigger_pattern": "pin_bar",
                }

            # (rsi_divergence pattern removed 2026-07-06: 1W/6L, 14% WR, -$330 —
            # RSI extremes on gold mark continuation more often than reversal.)

        # ── SELL setups at resistance ─────────────────────────────────────────
        if lp > current_price:
            # Bullish structural zones (OB/FVG created on an up-move) must not
            # be used as sell resistance — they are BUY zones by design.
            if lv_type in ("ob_bull", "fvg_bull"):
                continue

            price_tested = last_high >= lp - zone_width * 0.5
            if lv_type == "ob_bear":
                price_tested = price_tested or (current_price >= lp - atr * 0.6)
            if not price_tested:
                continue

            counter_ok = (ap.get("allow_counter_bias") >= 0.5
                          or _counter_bias_allowed(session, lv_type, regime))
            bias_ok = counter_ok or htf_bias not in ("bullish",)

            # Pattern 5: Liquidity sweep at resistance — range/neutral days only
            if _sweep_ok and _is_liquidity_sweep_bear(last_c, prev_c, lp) and lv_str >= 2:
                return {
                    "direction": "SELL",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 2,
                    "price": current_price,
                    "zone_low":  round(lp - atr * 0.35, 2),
                    "zone_high": round(lp + 0.5, 2),
                    "trigger_pattern": "liquidity_sweep",
                }

            if not bias_ok:
                continue

            # Pattern 1: two bearish candles — range days only, strength >= 3
            if (_bounce_ok
                    and _candle_is_bearish(last_c) and _candle_is_bearish(prev_c)
                    and lv_str >= 3
                    and _candle_body_atr_ratio(last_c, atr) >= 0.15
                    and _macd_fading_for_sell(m15_candles)):
                return {
                    "direction": "SELL",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str,
                    "price": current_price,
                    "zone_low":  round(lp - atr * 0.4, 2),
                    "zone_high": round(lp + 1.0, 2),
                    "trigger_pattern": "bounce",
                }

            # Pattern 2: bearish engulfing at resistance
            if _englf_ok and _is_bearish_engulfing(last_c, prev_c):
                return {
                    "direction": "SELL",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 1,
                    "price": current_price,
                    "zone_low":  round(lp - atr * 0.4, 2),
                    "zone_high": round(lp + 1.0, 2),
                    "trigger_pattern": "engulfing",
                }

            # Pattern 3: shooting star at resistance
            if _englf_ok and _is_shooting_star(last_c):
                return {
                    "direction": "SELL",
                    "key_level": lp, "key_level_type": lv_type,
                    "level_strength": lv_str + 1,
                    "price": current_price,
                    "zone_low":  round(lp - atr * 0.4, 2),
                    "zone_high": round(lp + 1.0, 2),
                    "trigger_pattern": "pin_bar",
                }

            # (rsi_divergence pattern removed — see BUY side note.)

    return None


# ── Risk levels ───────────────────────────────────────────────────────────────

def calculate_risk_levels(
    candidate: dict,
    atr_m15: float,
    key_levels: list[dict],
    direction: str,
    regime: str = "neutral",
) -> Optional[dict]:
    """
    Compute SL and 3 TP levels from the candidate signal.
    SL: level + sl_atr_mult×ATR buffer (just beyond the structure)
    TP1: min_rr × SL distance (default 1:1 R:R)
    TP2: (min_rr + 0.8) × SL distance
    TP3: next key level OR (min_rr + 2.0) × SL distance

    sl_atr_mult and min_rr are learned per-regime (see adaptive_params) —
    a trending day tightening these doesn't affect ranging-day behaviour.

    Returns None if minimum R:R cannot be achieved.
    """
    zone_low  = candidate["zone_low"]
    zone_high = candidate["zone_high"]
    entry_mid = round((zone_low + zone_high) / 2, 2)
    sl_buffer = round(atr_m15 * ap.get("sl_atr_mult", regime=regime), 2)

    min_rr_val = ap.get("min_rr", regime=regime)

    if direction == "BUY":
        sl       = round(candidate["key_level"] - sl_buffer, 2)
        sl_dist  = round(entry_mid - sl, 2)
        if sl_dist <= 0:
            return None
        tp1 = round(entry_mid + sl_dist * min_rr_val, 2)
        tp2 = round(entry_mid + sl_dist * (min_rr_val + 0.8), 2)
        next_res = next(
            (lv["price"] for lv in sorted(key_levels, key=lambda x: x["price"])
             if lv["price"] > tp1 + 2.0),
            None,
        )
        tp3 = next_res if next_res else round(entry_mid + sl_dist * (min_rr_val + 2.0), 2)

    else:  # SELL
        sl       = round(candidate["key_level"] + sl_buffer, 2)
        sl_dist  = round(sl - entry_mid, 2)
        if sl_dist <= 0:
            return None
        tp1 = round(entry_mid - sl_dist * min_rr_val, 2)
        tp2 = round(entry_mid - sl_dist * (min_rr_val + 0.8), 2)
        next_sup = next(
            (lv["price"] for lv in sorted(key_levels, key=lambda x: -x["price"])
             if lv["price"] < tp1 - 2.0),
            None,
        )
        tp3 = next_sup if next_sup else round(entry_mid - sl_dist * (min_rr_val + 2.0), 2)

    rr_tp1 = round(abs(tp1 - entry_mid) / sl_dist, 2) if sl_dist > 0 else 0
    rr_tp3 = round(abs(tp3 - entry_mid) / sl_dist, 2) if sl_dist > 0 else 0

    if rr_tp1 < min_rr_val - 0.01:   # small tolerance for float rounding
        return None

    return {
        "entry_low":  zone_low,
        "entry_high": zone_high,
        "entry_mid":  entry_mid,
        "stop_loss":  sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "sl_dist":  sl_dist,
        "rr_tp1":   rr_tp1,
        "rr_tp3":   rr_tp3,
    }


# ── H4 bias ───────────────────────────────────────────────────────────────────

def compute_h4_bias(h4_candles: list[dict]) -> str:
    """H4 HTF bias using EMA20/50 with full data warmup — same logic as H1."""
    if len(h4_candles) < 52:
        return "neutral"
    closes = [float(c["close"]) for c in h4_candles if c.get("close")]
    if len(closes) < 52:
        return "neutral"
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    price = closes[-1]
    if price > ema20 > ema50:
        return "bullish"
    if price < ema20 < ema50:
        return "bearish"
    return "neutral"


# ── ADX ───────────────────────────────────────────────────────────────────────

def compute_adx(candles: list[dict], period: int = 14) -> float:
    """
    ADX using Wilder's smoothing.  Returns 0-100.
    >25 = trending, <20 = ranging.
    """
    if len(candles) < period + 2:
        return 20.0

    highs  = [float(c.get("high",  0) or 0) for c in candles]
    lows   = [float(c.get("low",   0) or 0) for c in candles]
    closes = [float(c.get("close", 0) or 0) for c in candles]

    tr_list: list[float] = []
    dm_plus: list[float] = []
    dm_minus: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up   = highs[i]      - highs[i - 1]
        down = lows[i - 1]   - lows[i]
        dm_plus.append(up   if up   > down and up   > 0 else 0.0)
        dm_minus.append(down if down > up   and down > 0 else 0.0)
        tr_list.append(tr)

    if len(tr_list) < period:
        return 20.0

    alpha = 1.0 / period

    def _wilder(vals: list[float]) -> list[float]:
        result = [sum(vals[:period])]
        for v in vals[period:]:
            result.append(result[-1] * (1 - alpha) + v)
        return result

    atr14 = _wilder(tr_list)
    pdm14 = _wilder(dm_plus)
    mdm14 = _wilder(dm_minus)

    dx_list: list[float] = []
    for a, p, m in zip(atr14, pdm14, mdm14):
        if a < 1e-9:
            continue
        pdi   = 100 * p / a
        mdi   = 100 * m / a
        denom = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / denom if denom > 1e-9 else 0.0)

    if not dx_list:
        return 20.0
    return round(sum(dx_list[-period:]) / min(len(dx_list), period), 2)


# ── MACD histogram ────────────────────────────────────────────────────────────

def compute_macd_hist(closes: list[float]) -> tuple[float, float]:
    """
    MACD(12, 26, 9).  Returns (macd_line, histogram).
    Requires at least 35 closes.
    """
    if len(closes) < 35:
        return 0.0, 0.0
    n = len(closes)
    macd_vals: list[float] = []
    for end in range(n - 8, n + 1):
        if end < 26:
            macd_vals.append(0.0)
            continue
        seg = closes[max(0, end - 50):end]
        macd_vals.append(_ema(seg[-26:], 12) - _ema(seg[-26:], 26))
    macd_line   = macd_vals[-1]
    signal_line = _ema(macd_vals[-9:], 9) if len(macd_vals) >= 9 else macd_line
    return round(macd_line, 4), round(macd_line - signal_line, 4)


# ── Market regime ─────────────────────────────────────────────────────────────

def detect_regime(adx: float, h1_bias: str, h4_bias: str) -> str:
    """
    trending — ADX > 25 AND H1 and H4 agree on a non-neutral direction.
    ranging  — ADX < 20.
    neutral  — everything else.
    """
    if adx > 25 and h1_bias == h4_bias and h1_bias != "neutral":
        return "trending"
    if adx < 20:
        return "ranging"
    return "neutral"


# ── Session-aware counter-bias permission ─────────────────────────────────────

def _counter_bias_allowed(session: str, level_type: str, regime: str) -> bool:
    """
    Smart counter-bias: allowed in ranging regimes and during high-momentum
    open windows where fading the range/extreme is a valid institutional setup.

      - Range day: always allow (market is not trending, fade is valid)
      - London open 08-10 UTC: allow fading Asian range extremes
      - NY open 13-15 UTC: allow fading key structural levels
    """
    if regime in ("range", "ranging"):
        return True
    hour = datetime.now(timezone.utc).hour
    if 8 <= hour < 10 and level_type in ("asian_high", "asian_low"):
        return True
    if 13 <= hour < 15 and level_type in ("round", "daily_mid", "resistance", "support"):
        return True
    return False


# ── M5 scalp trigger ─────────────────────────────────────────────────────────

def check_scalp_trigger(
    m5_candles: list[dict],
    key_levels: list[dict],
    htf_bias: str,
    current_price: float,
    atr_m5: float,
    session: str,
) -> Optional[dict]:
    """
    Tight M5 bounce off a key level during high-momentum open windows only:
      - London open: 08-10 UTC
      - NY open:     13-15 UTC

    Requires strong candle confirmation (engulfing or pin bar) at a level
    with strength >= 2.  Never fires counter-trend in a trending regime.
    """
    hour = datetime.now(timezone.utc).hour
    if not ((8 <= hour < 10) or (13 <= hour < 15)):
        return None
    if len(m5_candles) < 4 or atr_m5 < 0.5:
        return None

    zone_width = max(atr_m5 * 0.35, 2.0)
    last_c = m5_candles[-1]
    prev_c = m5_candles[-2]
    last_low  = float(last_c.get("low",  0) or 0)
    last_high = float(last_c.get("high", 0) or 0)

    for level in key_levels[:10]:
        if level["strength"] < 2:
            continue
        lp   = level["price"]
        dist = abs(current_price - lp)
        if dist > zone_width:
            continue

        if lp < current_price:
            if last_low > lp + zone_width * 0.4:
                continue
            if htf_bias == "bearish":
                continue
            if _is_bullish_engulfing(last_c, prev_c) or _is_hammer(last_c):
                return {
                    "direction":      "BUY",
                    "key_level":      lp,
                    "key_level_type": level["type"],
                    "level_strength": level["strength"],
                    "price":          current_price,
                    "zone_low":       round(lp - 0.5, 2),
                    "zone_high":      round(lp + atr_m5 * 0.3, 2),
                    "trigger_pattern": "scalp",
                    "is_scalp":       True,
                }

        if lp > current_price:
            if last_high < lp - zone_width * 0.4:
                continue
            if htf_bias == "bullish":
                continue
            if _is_bearish_engulfing(last_c, prev_c) or _is_shooting_star(last_c):
                return {
                    "direction":      "SELL",
                    "key_level":      lp,
                    "key_level_type": level["type"],
                    "level_strength": level["strength"],
                    "price":          current_price,
                    "zone_low":       round(lp - atr_m5 * 0.3, 2),
                    "zone_high":      round(lp + 0.5, 2),
                    "trigger_pattern": "scalp",
                    "is_scalp":       True,
                }

    return None


def calculate_scalp_risk_levels(candidate: dict, atr_m5: float) -> Optional[dict]:
    """
    Tight risk levels for M5 scalps: SL 3-5 pts beyond the level, TP1 at 1.5× SL.
    """
    zone_low  = candidate["zone_low"]
    zone_high = candidate["zone_high"]
    entry_mid = round((zone_low + zone_high) / 2, 2)
    direction = candidate["direction"]

    sl_dist = max(3.0, min(5.0, atr_m5 * 0.5))

    if direction == "BUY":
        sl  = round(candidate["key_level"] - sl_dist, 2)
        tp1 = round(entry_mid + sl_dist * 1.5, 2)
        tp2 = round(entry_mid + sl_dist * 2.5, 2)
        tp3 = round(entry_mid + sl_dist * 3.0, 2)
        actual_sl_dist = round(entry_mid - sl, 2)
    else:
        sl  = round(candidate["key_level"] + sl_dist, 2)
        tp1 = round(entry_mid - sl_dist * 1.5, 2)
        tp2 = round(entry_mid - sl_dist * 2.5, 2)
        tp3 = round(entry_mid - sl_dist * 3.0, 2)
        actual_sl_dist = round(sl - entry_mid, 2)

    if actual_sl_dist <= 0:
        return None

    rr_tp1 = round(abs(tp1 - entry_mid) / actual_sl_dist, 2)
    rr_tp3 = round(abs(tp3 - entry_mid) / actual_sl_dist, 2)

    return {
        "entry_low":  zone_low,
        "entry_high": zone_high,
        "entry_mid":  entry_mid,
        "stop_loss":  sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "sl_dist":  actual_sl_dist,
        "rr_tp1":   rr_tp1,
        "rr_tp3":   rr_tp3,
    }
