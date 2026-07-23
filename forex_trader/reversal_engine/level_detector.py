"""
Level detection for Reversal Engine.

Identifies key support/resistance levels using two complementary
methodologies, both reverse-engineered from real channel history:

  Gold Diggers VIP style (original, from Asia-session behaviour):
    1. Asia session range (00:00-08:00 UTC)
    2. Previous swing high/low on H1
    3. Round numbers (nearest 5s and 10s)
    4. Recent congestion zones

  Gold Diggers 2.0 / Institutional style (added 2026-07, from four weeks of
  channel history including posted chart screenshots): a genuine ICT
  "Unicorn" setup — liquidity sweep, market structure shift, then an FVG
  overlapping a breaker block — see reversal_engine.ict_patterns for the
  detection logic and the evidence behind it (the channel's own charts run
  TradingView's FVG/iFVG indicator across multiple timeframes, mark equal
  highs/lows with "£" annotations, and one caption names the setup
  outright: "50% OF THE 15M UCC").

Levels are scored 0-1 and ranked. The engine signals when price approaches
a top-ranked level.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from forex_trader.reversal_engine import ict_patterns as ict

_log = logging.getLogger(__name__)

# How close price must be to a level before we generate a signal (pts)
PROXIMITY_THRESHOLD_PTS = 8.0

# Minimum price distance between two levels (to avoid clustering)
MIN_LEVEL_SEPARATION_PTS = 3.0

# Candle lookback for swing detection
SWING_LOOKBACK = 30

# Min/max candles on each side for a swing point
SWING_WINDOW = 3


def _utc_hour(candle: dict) -> int:
    """Extract UTC hour from candle time field (handles unix ts or ISO string)."""
    t = candle.get("time") or candle.get("ts") or 0
    try:
        if isinstance(t, str):
            return datetime.fromisoformat(t.replace("Z", "+00:00")).hour
        return datetime.fromtimestamp(float(t), tz=timezone.utc).hour
    except Exception:
        return 0


def get_asia_range(h1_candles: list[dict]) -> tuple[float, float]:
    """
    Return (asia_low, asia_high) from the most recent complete Asia session
    (00:00-08:00 UTC).  Falls back to (0, 0) if no Asia candles found.
    """
    if not h1_candles:
        return 0.0, 0.0

    # Find the latest Asia session candles
    asia = [c for c in h1_candles if _utc_hour(c) < 8]
    if not asia:
        return 0.0, 0.0

    lows  = [float(c.get("low",  c.get("l", 0))) for c in asia]
    highs = [float(c.get("high", c.get("h", 0))) for c in asia]
    lows  = [x for x in lows  if x > 0]
    highs = [x for x in highs if x > 0]
    if not lows or not highs:
        return 0.0, 0.0

    return round(min(lows), 2), round(max(highs), 2)


def get_swing_levels(h1_candles: list[dict], lookback: int = SWING_LOOKBACK) -> list[dict]:
    """
    Identify significant swing highs and lows on H1 candles.
    Returns list of {price, type, strength, candle_index}.
    """
    if len(h1_candles) < SWING_WINDOW * 2 + 1:
        return []

    candles = h1_candles[-lookback:] if len(h1_candles) > lookback else h1_candles
    highs = [float(c.get("high", c.get("h", 0))) for c in candles]
    lows  = [float(c.get("low",  c.get("l", 0))) for c in candles]
    levels = []
    w = SWING_WINDOW

    for i in range(w, len(candles) - w):
        h = highs[i]
        l = lows[i]
        if h <= 0 or l <= 0:
            continue

        # Swing high: highest in [i-w .. i+w]
        if all(h >= highs[j] for j in range(i - w, i + w + 1) if j != i):
            strength = sum(1 for j in range(i - w, i + w + 1) if j != i and highs[j] < h)
            levels.append({
                "price":  round(h, 2),
                "type":   "swing_high",
                "strength": strength,
                "idx":    i,
            })

        # Swing low: lowest in [i-w .. i+w]
        if all(l <= lows[j] for j in range(i - w, i + w + 1) if j != i):
            strength = sum(1 for j in range(i - w, i + w + 1) if j != i and lows[j] > l)
            levels.append({
                "price":  round(l, 2),
                "type":   "swing_low",
                "strength": strength,
                "idx":    i,
            })

    # De-duplicate: merge levels within MIN_LEVEL_SEPARATION_PTS
    merged = _deduplicate_levels(levels)
    return merged


CONGESTION_WINDOW     = 5    # rolling window size (candles) checked for consolidation
CONGESTION_RANGE_MULT = 2.5  # window's overall high-low range must be <= this many x
                              # the window's own average single-candle range to count
                              # as "tight" -- a trending window's overall range grows
                              # much faster than its average single-candle range,
                              # since each candle adds net directional movement rather
                              # than overlapping the last one. Not backtested against
                              # real channel history the way score_level()'s type
                              # weights were (see that function's docstring) -- a
                              # reasoned starting heuristic, worth recalibrating once
                              # enough real congestion-level signals have closed.


def get_congestion_zones(h1_candles: list[dict], lookback: int = SWING_LOOKBACK) -> list[dict]:
    """
    Identify recent congestion/consolidation zones -- the fourth REF-style
    level source alongside Asia range, swing points, and round numbers (see
    module docstring). Documented and scored (score_level's "congestion"
    type, 0.65 base) and referenced in ml_engine.FEATURE_NAMES since
    inception, but never actually detected until now -- get_candidate_levels()
    only ever built asia/swing/round/unicorn candidates.

    A window of CONGESTION_WINDOW candles counts as a congestion zone when
    its overall high-low range is tight relative to its own average single-
    candle range (see CONGESTION_RANGE_MULT). Non-overlapping windows are
    scanned left to right; once a zone is found the scan resumes after it
    rather than reporting overlapping sub-zones of the same consolidation.

    Returns level dicts shaped like get_swing_levels()'s output:
    {price, type="congestion", strength, idx} -- price is the zone's
    midpoint.
    """
    if len(h1_candles) < CONGESTION_WINDOW:
        return []

    candles = h1_candles[-lookback:] if len(h1_candles) > lookback else h1_candles
    highs = [float(c.get("high", c.get("h", 0))) for c in candles]
    lows  = [float(c.get("low",  c.get("l", 0))) for c in candles]
    n = len(candles)
    if n < CONGESTION_WINDOW:
        return []

    zones = []
    i = 0
    while i <= n - CONGESTION_WINDOW:
        window_highs = highs[i:i + CONGESTION_WINDOW]
        window_lows  = lows[i:i + CONGESTION_WINDOW]
        if any(h <= 0 for h in window_highs) or any(l <= 0 for l in window_lows):
            i += 1
            continue

        window_high = max(window_highs)
        window_low  = min(window_lows)
        window_range = window_high - window_low
        avg_single_range = sum(
            h - l for h, l in zip(window_highs, window_lows)
        ) / CONGESTION_WINDOW
        if avg_single_range <= 0:
            i += 1
            continue

        if window_range <= avg_single_range * CONGESTION_RANGE_MULT:
            zones.append({
                "price":    round((window_high + window_low) / 2, 2),
                "type":     "congestion",
                "strength": CONGESTION_WINDOW,
                "idx":      i,
            })
            i += CONGESTION_WINDOW  # don't report overlapping sub-zones of the same consolidation
        else:
            i += 1

    return _deduplicate_levels(zones)


def get_round_levels(price: float, count: int = 6) -> list[dict]:
    """
    Return the nearest round-10 and round-5 levels around current price.
    """
    if price <= 0:
        return []
    levels = []
    base_10 = round(price / 10) * 10
    for offset in range(-count, count + 1):
        r10 = base_10 + offset * 10
        if r10 > 0:
            levels.append({"price": float(r10), "type": "round_10", "strength": 3})
        # Round-5 midpoints (e.g. 4005, 4015 ...)
        r5 = r10 + 5
        if r5 > 0:
            levels.append({"price": float(r5), "type": "round_5", "strength": 2})

    return levels


def _deduplicate_levels(levels: list[dict]) -> list[dict]:
    """Merge levels that are within MIN_LEVEL_SEPARATION_PTS of each other."""
    if not levels:
        return []
    sorted_lvls = sorted(levels, key=lambda x: x["price"])
    merged = [sorted_lvls[0]]
    for lvl in sorted_lvls[1:]:
        if abs(lvl["price"] - merged[-1]["price"]) < MIN_LEVEL_SEPARATION_PTS:
            # Keep the one with higher strength; boost combined strength
            if lvl["strength"] > merged[-1]["strength"]:
                merged[-1] = lvl
            merged[-1]["strength"] += 1
        else:
            merged.append(lvl)
    return merged


def score_level(level: dict, current_price: float, htf_bias: str,
                asia_low: float, asia_high: float) -> float:
    """
    Score a candidate level 0.0-1.0.

    Higher scores = more likely the reference channel would trade this level.

    Recalibrated 2026-07-16 from a direct backtest of 767 real, fully-parsed
    Gold Diggers VIP/GD2 signal entries (2026-06-04 to 2026-07-15) against a
    randomized-timestamp control, plus 387 of those signals' actual own
    executed trade outcomes:

      type          real-signal hit rate   vs random control   outcome $ edge
      asia_low/high        3.5%                +1.7pt          not measured (too rare)
      swing_high/low       40.0%               +2.3pt          none (62.7% WR near vs 62.5% away)
      round_10             42.0%               +1.2pt          not isolated
      round_5              46.3%               +9.0pt          real (avg $0.17/trade near
                                                                  vs -$1.23/trade away)

    The previous weights had this backwards: Asia range (rarest, weakest
    edge) was weighted highest at 0.90, swing levels (no measured outcome
    edge at all) were second at 0.75, and round numbers (the only feature
    with both a real statistical lift over random AND a real $ outcome
    edge) were weighted lowest at 0.50-0.60. Round numbers now outrank
    swing and Asia-range candidates when multiple sit near price at once;
    Asia range keeps a modest weight (it's still a real, if rare, signal)
    rather than dominating on the rare occasions it applies.
    """
    price     = level["price"]
    ltype     = level.get("type", "unknown")
    strength  = level.get("strength", 1)
    dist_pts  = abs(current_price - price)

    # Base score by type
    type_scores = {
        "asia_low":    0.60,
        "asia_high":   0.60,
        "swing_high":  0.55,
        "swing_low":   0.55,
        "round_10":    0.72,
        "round_5":     0.78,
        "congestion":  0.65,
    }
    score = type_scores.get(ltype, 0.50)

    # Strength bonus (up to +0.15)
    score += min(strength * 0.02, 0.15)

    # Proximity bonus: closer = more likely to be the next target
    # Sweet spot is 3-15 pts from current price (not too close, not too far)
    if 3 <= dist_pts <= 15:
        score += 0.10
    elif dist_pts < 3:
        score -= 0.20  # too close, already at level
    elif dist_pts > 30:
        score -= 0.15  # too far for scalping

    # Bias alignment: for swing_high as support, expect BUY bias
    if ltype == "swing_high" and htf_bias == "bullish":
        score += 0.05
    elif ltype == "swing_low" and htf_bias == "bearish":
        score += 0.05

    # Asia level bonus if it aligns with bias
    if ltype == "asia_low" and htf_bias in ("bullish", "neutral"):
        score += 0.05
    elif ltype == "asia_high" and htf_bias in ("bearish", "neutral"):
        score += 0.05

    # Round number proximity bonus (levels near a round number are more significant)
    nearest_10 = round(price / 10) * 10
    if abs(price - nearest_10) < 2:
        score += 0.05
    elif abs(price - (nearest_10 + 5)) < 2:
        score += 0.03

    return round(min(max(score, 0.0), 1.0), 3)


def get_unicorn_candidates(m15_candles: list[dict], current_price: float) -> list[dict]:
    """
    GD2/Institutional-style candidate: a confirmed ICT Unicorn setup
    (liquidity sweep -> structure shift -> FVG/breaker confluence) on M15.

    Unlike the REF-style levels below (a static price the engine waits for
    price to *approach*), a unicorn setup is only reported once fully
    confirmed — it's already a live, directional trigger, not a level to
    wait on. Scored high (0.80 base) since all three ICT stages already
    agreed; entry_zone_low/high carry the actual confluence zone rather
    than a single point + ATR padding.
    """
    if not m15_candles or len(m15_candles) < 25 or current_price <= 0:
        return []
    setup = ict.find_unicorn_setup(m15_candles)
    if not setup:
        return []
    direction = "BUY" if setup["direction"] == "bullish" else "SELL"
    score = 0.80 + (0.10 if setup["confluence"] else 0.0)
    return [{
        "price": setup["entry_mid"],
        "type": "unicorn",
        "strength": 5,
        "score": round(min(score, 1.0), 3),
        "direction": direction,
        "distance_pts": round(abs(current_price - setup["entry_mid"]), 2),
        "profile": "gd2",
        "entry_zone_low": setup["zone_low"],
        "entry_zone_high": setup["zone_high"],
        "sweep_price": setup["sweep"]["price"],
        "confluence": setup["confluence"],
    }]


def get_all_levels(h1_candles: list[dict], current_price: float) -> list[dict]:
    """
    Every known candidate S/R level (asia/swing/round/congestion), deduped,
    with no proximity or score filtering applied -- the raw material
    get_candidate_levels() scores and truncates from. Exposed separately so
    callers that need the full picture (e.g. classifying an arbitrary REF
    price against "what levels exist right now", not just the handful the
    bot itself was close enough to act on) don't have to reimplement the
    asia+swing+round+congestion assembly themselves.
    """
    if not h1_candles or current_price <= 0:
        return []

    asia_low, asia_high = get_asia_range(h1_candles)
    swing_levels        = get_swing_levels(h1_candles)
    round_levels        = get_round_levels(current_price)
    congestion_zones     = get_congestion_zones(h1_candles)

    candidates: list[dict] = []

    # Asia levels
    if asia_low > 0:
        candidates.append({"price": asia_low,  "type": "asia_low",  "strength": 5})
    if asia_high > 0:
        candidates.append({"price": asia_high, "type": "asia_high", "strength": 5})

    # Swing levels
    candidates.extend(swing_levels)

    # Congestion zones (only those within 50pts of price, same window as round numbers)
    for z in congestion_zones:
        if abs(z["price"] - current_price) <= 50:
            candidates.append(z)

    # Round number levels (only those within 50pts of price)
    for r in round_levels:
        if abs(r["price"] - current_price) <= 50:
            candidates.append(r)

    # Deduplicate across all sources
    return _deduplicate_levels(candidates)


def get_candidate_levels(
    h1_candles: list[dict],
    current_price: float,
    htf_bias: str = "neutral",
    max_candidates: int = 8,
    m15_candles: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Main entry point. Returns the top candidate S/R levels sorted by score.

    Each returned level dict contains:
      price, type, strength, score, direction, distance_pts
    Unicorn candidates (see get_unicorn_candidates) additionally carry
    profile='gd2' and their own entry_zone_low/high.
    """
    if not h1_candles or current_price <= 0:
        return []

    asia_low, asia_high = get_asia_range(h1_candles)
    candidates = get_all_levels(h1_candles, current_price)

    # Score and annotate
    scored = []
    for lvl in candidates:
        dist = abs(current_price - lvl["price"])
        if dist < 1.0:
            continue  # already inside the level

        s = score_level(lvl, current_price, htf_bias, asia_low, asia_high)
        lvl["score"]        = s
        lvl["distance_pts"] = round(dist, 2)

        # Assign trading direction: price above level → BUY (expect bounce up)
        # price below level → SELL (expect bounce down)
        if current_price > lvl["price"]:
            lvl["direction"] = "BUY"
        else:
            lvl["direction"] = "SELL"

        # Only include levels within reasonable proximity for scalping
        if dist <= PROXIMITY_THRESHOLD_PTS:
            scored.append(lvl)

    # Unicorn (GD2-style) candidates are already-confirmed triggers, not
    # levels waiting to be approached — added after the proximity filter
    # above so they're never accidentally excluded by it.
    scored.extend(get_unicorn_candidates(m15_candles or [], current_price))

    scored.sort(key=lambda x: -x["score"])
    return scored[:max_candidates]


def get_htf_bias(h1_candles: list[dict], h4_candles: Optional[list[dict]] = None) -> str:
    """
    Determine HTF directional bias from H1 (and optionally H4) price structure.
    Higher highs + higher lows = bullish. Lower highs + lower lows = bearish.
    Returns 'bullish', 'bearish', or 'neutral'.
    """
    if not h1_candles or len(h1_candles) < 10:
        return "neutral"

    candles = h1_candles[-20:] if len(h1_candles) > 20 else h1_candles
    highs = [float(c.get("high", c.get("h", 0))) for c in candles]
    lows  = [float(c.get("low",  c.get("l", 0))) for c in candles]

    # Simple higher-high / higher-low check over rolling 5-candle windows
    n = len(highs)
    if n < 8:
        return "neutral"

    mid = n // 2
    h_first  = max(highs[:mid])
    h_second = max(highs[mid:])
    l_first  = min(lows[:mid])
    l_second = min(lows[mid:])

    hh = h_second > h_first
    hl = l_second > l_first
    lh = h_second < h_first
    ll = l_second < l_first

    if hh and hl:
        bias = "bullish"
    elif lh and ll:
        bias = "bearish"
    else:
        bias = "neutral"

    # If H4 candles available, weight them
    if h4_candles and len(h4_candles) >= 6:
        h4h = [float(c.get("high", c.get("h", 0))) for c in h4_candles[-6:]]
        h4l = [float(c.get("low",  c.get("l", 0))) for c in h4_candles[-6:]]
        h4_hh = h4h[-1] > max(h4h[:-1]) if h4h[:-1] else True
        h4_hl = h4l[-1] > min(h4l[:-1]) if h4l[:-1] else True
        if h4_hh and h4_hl and bias != "bullish":
            bias = "neutral"  # H4 says bullish but H1 doesn't confirm → neutral
        elif not h4_hh and not h4_hl and bias != "bearish":
            bias = "neutral"

    return bias


def get_session(utc_hour: int) -> str:
    """Map UTC hour to trading session."""
    if 0 <= utc_hour < 8:
        return "asian"
    if 8 <= utc_hour < 12:
        return "london"
    if 12 <= utc_hour < 17:
        return "overlap"
    if 17 <= utc_hour < 21:
        return "ny"
    return "off"


