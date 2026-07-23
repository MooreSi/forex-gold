"""
Signal construction for Reversal Engine.

Builds signals that match the Gold Diggers VIP format exactly:
  - 2-4pt entry zone
  - SL 4pts below zone (modal value from 294-signal analysis)
  - TP1-TP8 cascade matching the reference channel spacing pattern
"""
from __future__ import annotations

import hashlib
import time


# the reference channel modal SL distances by bias strength
_SL_DIST_BY_SCORE = {
    "high":   7,   # score > 0.85 — wider SL for strong levels
    "medium": 5,   # score 0.65-0.85
    "low":    4,   # score < 0.65 — tight SL
}

# TP cascade offsets from entry_mid (points)
# Derived from the reference channel TP spacing analysis: TP1-TP7 gaps [2,2,2,2,2,4] or [1,1,1,2,3,5]
# We use a slightly wider cascade to capture more of the move
_TP_OFFSETS = [3, 4, 5, 6, 8, 10, 15, 30]

# Entry zone half-width
_ZONE_HALF = 2.0  # 4pt zone total (matches the reference channel median 3pt but rounds nicely)


def make_signal_ref() -> str:
    raw = f"RE-{time.time()}"
    return "RE-" + hashlib.md5(raw.encode()).hexdigest()[:6].upper()


def entry_zone_from_level(level_price: float, atr: float, direction: str) -> tuple[float, float]:
    """
    Create entry zone around a key level.

    For BUY: zone sits just above the level (buying the pullback into support)
    For SELL: zone sits just below the level (selling the rally into resistance)
    """
    # Widen zone slightly at high ATR (volatile markets need a wider zone)
    zone_half = _ZONE_HALF
    if atr > 15:
        zone_half = 3.0

    if direction == "BUY":
        entry_low  = round(level_price - 1.0,  2)
        entry_high = round(level_price + zone_half, 2)
    else:
        entry_high = round(level_price + 1.0,  2)
        entry_low  = round(level_price - zone_half, 2)

    return entry_low, entry_high


def calculate_tp_cascade(direction: str, entry_mid: float, sl_dist: float) -> dict:
    """
    Build the 8-level TP cascade matching the reference channel format.

    TPs are placed at fixed offsets from entry mid.
    Uses the reference channel observed spacing but scales with sl_dist for proportionality.
    """
    tps = {}
    for i, offset in enumerate(_TP_OFFSETS, start=1):
        if direction == "BUY":
            tps[f"tp{i}"] = round(entry_mid + offset, 2)
        else:
            tps[f"tp{i}"] = round(entry_mid - offset, 2)
    return tps


# GD2/Institutional TP structure — NOT the REF 8-level ladder. Four weeks of
# real follow-up messages show a scale-out-then-breakeven playbook instead:
# "+30 pips from best entries, making my trade risk free by taking partial
# profits" (TP1, partial + SL->BE), "Close your first entries and set
# breakeven on the best entries" (repeats the same pattern), then a runner
# left open with no further fixed level ("Running +200 from best entries").
# TP1/TP2 are risk-multiples of the trade's own SL distance (so they scale
# with how wide the confluence zone/stop actually is, not a fixed point
# offset); the "runner" target is a wide multiple matching their observed
# bigger wins (100-250+ pips on trend days).
_GD2_TP1_R  = 1.0   # first partial close, matches sl_dist 1:1
_GD2_TP2_R  = 2.0   # second partial + move SL to breakeven
_GD2_RUNNER_R = 6.0  # final runner target, no further management


def calculate_gd2_tp_structure(direction: str, entry_mid: float, sl_dist: float) -> dict:
    sign = 1.0 if direction == "BUY" else -1.0
    return {
        "tp1": round(entry_mid + sign * _GD2_TP1_R * sl_dist, 2),
        "tp2": round(entry_mid + sign * _GD2_TP2_R * sl_dist, 2),
        "tp3": round(entry_mid + sign * _GD2_RUNNER_R * sl_dist, 2),
    }


def sl_distance_for_level(level_score: float) -> float:
    """Return the SL distance in points based on the level quality score."""
    if level_score >= 0.85:
        return float(_SL_DIST_BY_SCORE["high"])
    if level_score >= 0.65:
        return float(_SL_DIST_BY_SCORE["medium"])
    return float(_SL_DIST_BY_SCORE["low"])


def build_signal(level: dict, direction: str, context: dict) -> dict:
    """
    Construct a complete signal dict ready for database insertion.

    Args:
        level:     candidate level dict from level_detector
        direction: 'BUY' or 'SELL'
        context:   dict with atr, adx, htf_bias, h1_bias, session, price_at_signal
    """
    atr            = float(context.get("atr", 8.0) or 8.0)
    level_price    = float(level["price"])
    level_score    = float(level.get("score", 0.5))
    is_gd2         = level.get("profile") == "gd2"

    if is_gd2:
        # Unicorn levels already carry a real confluence zone (FVG x breaker
        # block) -- use it directly instead of a synthetic point + ATR pad.
        entry_low  = float(level.get("entry_zone_low", level_price))
        entry_high = float(level.get("entry_zone_high", level_price))
        entry_mid  = round((entry_low + entry_high) / 2, 2)
        sl_dist    = max(abs(entry_high - entry_low), sl_distance_for_level(level_score))
    else:
        entry_low, entry_high = entry_zone_from_level(level_price, atr, direction)
        entry_mid  = round((entry_low + entry_high) / 2, 2)
        sl_dist    = sl_distance_for_level(level_score)

    if direction == "BUY":
        stop_loss = round(entry_mid - sl_dist, 2)
    else:
        stop_loss = round(entry_mid + sl_dist, 2)

    if is_gd2:
        tps = calculate_gd2_tp_structure(direction, entry_mid, sl_dist)
    else:
        tps = calculate_tp_cascade(direction, entry_mid, sl_dist)

    tp1_dist = abs(tps["tp1"] - entry_mid)
    rr_tp1   = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 0.0

    return {
        "signal_ref":      make_signal_ref(),
        "created_at":      time.time(),
        "direction":       direction,
        "entry_low":       entry_low,
        "entry_high":      entry_high,
        "stop_loss":       stop_loss,
        "sl_dist":         sl_dist,
        "rr_tp1":          rr_tp1,
        "level_price":     level_price,
        "level_type":      level.get("type", "unknown"),
        "level_score":     level_score,
        "session":         context.get("session", ""),
        "htf_bias":        context.get("htf_bias", ""),
        "h1_bias":         context.get("h1_bias", ""),
        "atr":             atr,
        "adx":             float(context.get("adx", 0) or 0),
        "price_at_signal": float(context.get("price_at_signal", 0) or 0),
        "status":          "pending",
        "outcome":         "open",
        "strategy":        "gd2_unicorn" if is_gd2 else "signal_climber",
        # Exact group_name string used by vantage_tg_signals/telegram_messages
        # for the real channel this signal is modelled on -- engine.py's
        # correlation check matches against this, not a display-friendly name.
        "source_channel":  "GOLD DIGGERS 2.0 ⚡️" if is_gd2 else "Gold Diggers VIP",
        **tps,
    }


def lot_size_for_risk(risk_dollars: float, sl_dist: float,
                      point_value: float = 1.0) -> float:
    """Calculate lot size to risk `risk_dollars` on a `sl_dist`-point SL."""
    if sl_dist <= 0 or point_value <= 0:
        return 0.01
    raw = risk_dollars / (sl_dist * point_value * 100)
    # Round to nearest 0.01 lot, clamp to broker limits
    lot = round(raw / 0.01) * 0.01
    return max(0.01, min(lot, 10.0))
