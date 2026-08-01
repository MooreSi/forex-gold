"""Risk levels, higher-timeframe indicators and the M5 scalp trigger --
split verbatim from signal_generator.py (M2 file-size pass).
signal_generator.py re-exports every public name so existing importers
(test_signal_generate, the breakout engine's clones) keep working unchanged.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from backend.src.services.dpm.engine import compute_atr
from backend.src.services.test_signal import adaptive_params as ap
from backend.src.services.test_signal.signal_generator import (
    _ema, _is_bearish_engulfing, _is_bullish_engulfing,
    _is_hammer, _is_shooting_star,
)

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
