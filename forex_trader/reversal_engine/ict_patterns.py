"""
ICT/Smart-Money-Concept pattern detection, shared by both Reversal Engine profiles
(Gold Diggers VIP and Gold Diggers 2.0 / Institutional).

Reverse-engineered 2026-07 from four weeks of real channel history (message
text, follow-ups, and posted chart screenshots) plus external research into
the specific tools the channels' charts show them using:
  - Chart screenshots consistently show TradingView's "FVG/iFVG (Nephew_Sam_)"
    indicator active across 15m/1h/4h/D, and manual "£" / "£££" annotations
    marking equal highs/lows (liquidity pools).
  - One caption explicitly names the setup: "PERFECT SNIPER ENTRY ... 50% OF
    THE 15M UCC" — UCC = ICT's "Unicorn Candle Concept": a liquidity sweep,
    followed by a market structure shift, leaving a Fair Value Gap that
    overlaps a breaker block (a violated former swing point); the overlap
    ("confluence zone") is the entry, typically taken at its 50% midpoint.
  - Follow-up messages ("I am making my trade risk free by taking partial
    profits", "Close your first entries and set breakeven on the best
    entries", "+30 pips from best entries") show staged entries across a
    zone and a partial-then-breakeven management style, not a fixed
    TP1-TP8 ladder with programmatic SL steps.

This module implements the standard, publicly-documented ICT definitions of
FVG/iFVG, equal-highs/lows liquidity pools, sweeps, and breaker blocks from
plain OHLC data — it does not depend on or reproduce any proprietary
indicator code.
"""
from __future__ import annotations

from typing import Optional


def _hi(c: dict) -> float:
    return float(c.get("high", c.get("h", 0)) or 0)


def _lo(c: dict) -> float:
    return float(c.get("low", c.get("l", 0)) or 0)


def _cl(c: dict) -> float:
    return float(c.get("close", c.get("c", 0)) or 0)


def _op(c: dict) -> float:
    return float(c.get("open", c.get("o", 0)) or 0)


def detect_fvgs(candles: list[dict], min_gap_pts: float = 0.5) -> list[dict]:
    """
    Standard 3-candle Fair Value Gap detection.

    Bullish FVG: candle[i-2].high < candle[i].low (an up-move leaves a gap
    below the third candle). Bearish FVG: candle[i-2].low > candle[i].high.

    Each FVG tracks its own fill/inversion state by scanning every later
    candle in the same list:
      - "filled": a later candle's wick has traded back into the gap zone.
      - "inverted": a later candle *closed* all the way through the gap,
        past its far boundary — the standard iFVG trigger (a filled FVG
        that fully reverses meaning, now favouring the opposite direction).

    Returns list of {top, bottom, mid, direction, idx, filled, inverted}.
    """
    if len(candles) < 3:
        return []

    # Session/weekend gaps (Friday close -> Monday open) produce a huge but
    # meaningless "high-low gap" between two consecutive bars that has
    # nothing to do with an intrabar imbalance -- confirmed on real XAUUSD
    # M15 history, where one such gap was wrongly picked up as a 45-point
    # FVG. Bars more than 3x the typical spacing apart aren't a real FVG.
    gaps = [candles[i]["ts"] - candles[i - 1]["ts"] for i in range(1, len(candles))]
    typical_interval = sorted(gaps)[len(gaps) // 2] if gaps else 900

    out = []
    for i in range(2, len(candles)):
        c0, c2 = candles[i - 2], candles[i]
        if (c2["ts"] - c0["ts"]) > 3 * typical_interval:
            continue
        if _hi(c0) < _lo(c2) and (_lo(c2) - _hi(c0)) >= min_gap_pts:
            top, bottom = _lo(c2), _hi(c0)
            fvg = {"top": round(top, 2), "bottom": round(bottom, 2),
                   "mid": round((top + bottom) / 2, 2), "direction": "bullish",
                   "idx": i - 1, "filled": False, "inverted": False}
            _track_fill_and_inversion(fvg, candles, i + 1)
            out.append(fvg)
        elif _lo(c0) > _hi(c2) and (_lo(c0) - _hi(c2)) >= min_gap_pts:
            top, bottom = _lo(c0), _hi(c2)
            fvg = {"top": round(top, 2), "bottom": round(bottom, 2),
                   "mid": round((top + bottom) / 2, 2), "direction": "bearish",
                   "idx": i - 1, "filled": False, "inverted": False}
            _track_fill_and_inversion(fvg, candles, i + 1)
            out.append(fvg)
    return out


def _track_fill_and_inversion(fvg: dict, candles: list[dict], start_idx: int) -> None:
    top, bottom = fvg["top"], fvg["bottom"]
    for c in candles[start_idx:]:
        if _lo(c) <= top and _hi(c) >= bottom:
            fvg["filled"] = True
        if fvg["direction"] == "bullish" and _cl(c) < bottom:
            fvg["inverted"] = True
        elif fvg["direction"] == "bearish" and _cl(c) > top:
            fvg["inverted"] = True


def detect_equal_levels(candles: list[dict], lookback: int = 40,
                         tolerance_pts: float = 1.5, min_touches: int = 2) -> list[dict]:
    """
    Equal-highs / equal-lows liquidity pools — the "£" / "£££" annotations
    seen on the channels' own charts. A pool is 2+ swing extremes within
    `tolerance_pts` of each other; more touches = a bigger resting-liquidity
    magnet and a higher-quality sweep target.

    Returns list of {price, type ('eq_high'/'eq_low'), touches}.
    """
    if len(candles) < 5:
        return []
    recent = candles[-lookback:] if len(candles) > lookback else candles
    highs = sorted(set(round(_hi(c), 1) for c in recent), reverse=True)
    lows = sorted(set(round(_lo(c), 1) for c in recent))

    def _cluster(vals: list[float]) -> list[dict]:
        clusters: list[list[float]] = []
        for v in vals:
            placed = False
            for cl_ in clusters:
                if abs(cl_[-1] - v) <= tolerance_pts:
                    cl_.append(v)
                    placed = True
                    break
            if not placed:
                clusters.append([v])
        return [{"price": round(sum(cl_) / len(cl_), 2), "touches": len(cl_)}
                for cl_ in clusters if len(cl_) >= min_touches]

    eq_highs = [{"price": c["price"], "type": "eq_high", "touches": c["touches"]}
                for c in _cluster(highs)]
    eq_lows = [{"price": c["price"], "type": "eq_low", "touches": c["touches"]}
               for c in _cluster(lows)]
    return eq_highs + eq_lows


def detect_liquidity_sweep(candles: list[dict], pools: list[dict],
                            recent_n: int = 5) -> Optional[dict]:
    """
    A sweep = price wicks beyond a liquidity pool (taking the resting stops)
    then closes back on the origin side within the same or a following
    candle — the classic ICT stop-hunt-then-reversal signature.

    Only looks at the most recent `recent_n` candles so a sweep is reported
    close to when it actually happened, not any historical touch.

    Returns the swept pool plus direction of the *expected reversal*
    (sweeping a high implies a bearish reversal and vice versa), or None.
    """
    if not candles or not pools:
        return None
    window = candles[-recent_n:]
    for c in window:
        for pool in pools:
            if pool["type"] == "eq_high" and _hi(c) > pool["price"] and _cl(c) < pool["price"]:
                return {**pool, "reversal_direction": "bearish", "swept_at": _hi(c)}
            if pool["type"] == "eq_low" and _lo(c) < pool["price"] and _cl(c) > pool["price"]:
                return {**pool, "reversal_direction": "bullish", "swept_at": _lo(c)}
    return None


def detect_market_structure_shift(candles: list[dict], direction: str,
                                   lookback: int = 15) -> bool:
    """
    Confirms a structure shift in `direction` after a sweep: price must
    break the most recent opposing swing point (a lower-low for a bearish
    shift, a higher-high for a bullish shift) within the lookback window —
    the standard ICT "MSS" confirmation that separates a real reversal from
    a sweep that just continues the prior trend.
    """
    if len(candles) < lookback + 3:
        return False
    window = candles[-lookback:]
    highs = [_hi(c) for c in window]
    lows = [_lo(c) for c in window]
    last_close = _cl(window[-1])

    if direction == "bullish":
        # Break of the most recent minor swing high before the current push
        prior_high = max(highs[:-3]) if len(highs) > 3 else max(highs)
        return last_close > prior_high
    else:
        prior_low = min(lows[:-3]) if len(lows) > 3 else min(lows)
        return last_close < prior_low


def find_breaker_block(candles: list[dict], direction: str,
                        lookback: int = 20) -> Optional[dict]:
    """
    A breaker block: the last opposing-direction candle immediately before
    the impulsive move that broke structure — a failed swing point the
    market is expected to respect (as support if direction=='bullish',
    resistance if 'bearish') on a retest.
    """
    if len(candles) < 5:
        return None
    window = candles[-lookback:] if len(candles) > lookback else candles
    for i in range(len(window) - 1, 0, -1):
        c = window[i]
        prev = window[i - 1]
        is_up = _cl(c) > _op(c)
        is_down = _cl(c) < _op(c)
        if direction == "bullish" and is_down and _cl(window[min(i + 1, len(window) - 1)]) > _hi(prev):
            return {"low": round(_lo(c), 2), "high": round(_hi(c), 2), "idx": i}
        if direction == "bearish" and is_up and _cl(window[min(i + 1, len(window) - 1)]) < _lo(prev):
            return {"low": round(_lo(c), 2), "high": round(_hi(c), 2), "idx": i}
    return None


def find_unicorn_setup(candles: list[dict], pools: Optional[list[dict]] = None) -> Optional[dict]:
    """
    Full ICT "Unicorn" pipeline: liquidity sweep -> structure shift ->
    FVG/breaker-block confluence. Returns the confluence (entry) zone and
    direction if all three stages confirm on the given candles, else None.

    entry_mid is the 50% midpoint of the confluence zone, matching the
    channels' own "50% of the 15M UCC" entry convention.
    """
    if pools is None:
        pools = detect_equal_levels(candles)
    sweep = detect_liquidity_sweep(candles, pools)
    if not sweep:
        return None

    direction = sweep["reversal_direction"]
    if not detect_market_structure_shift(candles, direction):
        return None

    fvgs = detect_fvgs(candles)
    recent_fvgs = [f for f in fvgs if f["direction"] == direction and not f["inverted"]]
    if not recent_fvgs:
        return None
    fvg = recent_fvgs[-1]  # most recent matching FVG

    breaker = find_breaker_block(candles, direction)

    if breaker:
        lo = max(fvg["bottom"], breaker["low"])
        hi = min(fvg["top"], breaker["high"])
        if lo > hi:  # no actual overlap -- fall back to FVG alone
            lo, hi = fvg["bottom"], fvg["top"]
    else:
        lo, hi = fvg["bottom"], fvg["top"]

    # Defence in depth against a degenerate zone (e.g. a breaker block from
    # an unrelated swing, or any other edge case that slips past the
    # session-gap filter in detect_fvgs) -- the channels' own real entry
    # zones run a handful of points wide, never dozens. Fall back to the
    # FVG's own bounds, and give up entirely if even that is too wide to be
    # a genuine intrabar imbalance.
    _MAX_ZONE_PTS = 15.0
    _MIN_ZONE_PTS = 0.5
    if (hi - lo) > _MAX_ZONE_PTS or (hi - lo) < _MIN_ZONE_PTS:
        lo, hi = fvg["bottom"], fvg["top"]
        if (hi - lo) > _MAX_ZONE_PTS or (hi - lo) < _MIN_ZONE_PTS:
            return None

    return {
        "direction": direction,
        "sweep": sweep,
        "fvg": fvg,
        "breaker": breaker,
        "zone_low": round(lo, 2),
        "zone_high": round(hi, 2),
        "entry_mid": round((lo + hi) / 2, 2),
        "confluence": breaker is not None,
    }
