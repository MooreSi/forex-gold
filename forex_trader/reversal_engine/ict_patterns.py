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


def atr(candles: list[dict], period: int = 14) -> float:
    """Simple ATR over the last `period` bars. Used to size FVG thresholds
    relative to how much the instrument is actually moving."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(candles[-(period + 1):-1], candles[-period:]):
        pc = _cl(prev)
        trs.append(max(_hi(cur) - _lo(cur), abs(_hi(cur) - pc), abs(_lo(cur) - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def select_display_fvgs(candles: list[dict], fvgs: Optional[list[dict]] = None,
                        max_zones: int = 6, min_atr_frac: float = 0.25,
                        recent_bars: int = 300, show_inverted: bool = False) -> list[dict]:
    """Reduce raw `detect_fvgs` output to the few gaps a trader would actually
    have drawn on their chart.

    `detect_fvgs` is deliberately exhaustive because the ML features want
    every imbalance. A chart does not: on 300 bars of XAUUSD M15 it returns
    ~61 gaps, of which ~46 have already been closed clean through, and
    drawing all of them buries the price action under horizontal bands.

    Three rules, in the order they actually bite:
      - a zone dies when price CLOSES clean through its far edge (the
        `inverted` flag). A wick back into the gap is a test, not a death --
        that is exactly the retracement the zone exists to predict, so a
        tested-but-not-broken gap stays drawn. This is the dominant filter by
        a wide margin (61 -> 15 on the M15 sample below) and is deliberately
        NOT relaxed: an inverted FVG has flipped meaning, so still drawing it
        as a live zone in its original direction would be wrong, not merely
        cluttered;
      - size floor of `min_atr_frac` x ATR, so a 1pt gap in a 9pt-ATR market
        is not promoted to a level;
      - only the last `recent_bars` bars.

    Returns at most `max_zones`, most recent kept, in chart order. Returning
    nothing is a valid answer: sometimes there is no live gap worth drawing,
    and inventing one by relaxing the floor would defeat the point.

    RETUNED 2026-08-05 (was max_zones=4, min_atr_frac=0.50, recent_bars=120).
    The old numbers were calibrated against a reference screenshot carrying
    two zones over ~100 M15 bars; a later screenshot of the same reference
    chart showed five over a much longer span, so the target itself had
    moved. Measured on 300 bars of live XAUUSD (ATR 9.48 M15, 16.60 H1), the
    old constants were wrong in a specific way: the 0.50xATR floor sat ABOVE
    the median live-zone height (0.33xATR on M15), so it was discarding more
    than half of the zones that had survived the inversion rule -- 15 live
    gaps cut to 4. 0.25xATR sits below the median and keeps 9.

    recent_bars was doing almost nothing and now does nothing by design: at
    the old 0.50 floor every surviving zone was already inside the last 120
    bars, so the window never actually bound. Raising it to 300 matches the
    chart's own fetch depth (chart.py's _refresh_fvgs), which makes the fetch
    the single place the age horizon is set instead of having a second,
    tighter bound hidden here. An unmitigated gap does not expire just
    because price has been away from it for a while.

    Only the chart overlay calls this. The ML features go through
    `fvg_context`/`detect_fvgs` directly, so these numbers are display-only
    and changing them cannot move a trading decision.
    """
    if fvgs is None:
        fvgs = detect_fvgs(candles)
    if not fvgs:
        return []

    floor  = min_atr_frac * atr(candles)
    cutoff = len(candles) - recent_bars
    live = [f for f in fvgs
            if (show_inverted or not f["inverted"])
            and (f["top"] - f["bottom"]) >= floor
            and f["idx"] >= cutoff]

    picked = sorted(live, key=lambda f: f["idx"], reverse=True)[:max_zones]
    return sorted(picked, key=lambda f: f["idx"])


def fvg_context(candles: list[dict], entry: float, direction: str,
                atr: float = 5.0) -> dict:
    """Summarise the FVG picture around a proposed entry, as ML features.

    Added 2026-08-04. The reference channel visibly sets up from FVGs (their
    own chart screenshots run TradingView's FVG/iFVG indicator), so whether
    OUR reversal level coincides with an imbalance -- and whether that
    imbalance is still untested -- is exactly the kind of context the model
    had no way to see before.

    "Aligned" means the FVG points the same way as the trade: a BUY wants a
    bullish gap beneath it (unfilled demand), a SELL a bearish one above.

    Every value is normalised and has a defined meaning when NO gap is
    found, so a signal with no FVG nearby is never confused with one sitting
    in a fresh gap:
      fvg_confluence  1.0 entry inside an aligned gap, 0.5 inside an
                      opposing gap, 0.0 not inside any
      fvg_dist_norm   distance to the nearest aligned gap in ATR units,
                      clamped [0,5]; 5.0 (max = "far away") when none
      fvg_fresh       of that nearest aligned gap: 1.0 untested, 0.5 filled,
                      0.0 inverted; 0.5 (neutral) when none
      fvg_size_norm   its height in ATR units, clamped [0,3]; 0.0 when none
    """
    out = {"fvg_confluence": 0.0, "fvg_dist_norm": 5.0,
           "fvg_fresh": 0.5, "fvg_size_norm": 0.0}
    if not candles or entry <= 0:
        return out
    atr = max(float(atr or 0), 0.1)
    try:
        fvgs = detect_fvgs(candles)
    except Exception:
        return out
    if not fvgs:
        return out

    want = "bullish" if str(direction).upper() == "BUY" else "bearish"

    inside_aligned = any(f["bottom"] <= entry <= f["top"] and f["direction"] == want
                         and not f["inverted"] for f in fvgs)
    inside_any = any(f["bottom"] <= entry <= f["top"] for f in fvgs)
    out["fvg_confluence"] = 1.0 if inside_aligned else (0.5 if inside_any else 0.0)

    aligned = [f for f in fvgs if f["direction"] == want]
    if aligned:
        nearest = min(aligned, key=lambda f: abs(f["mid"] - entry))
        out["fvg_dist_norm"] = round(min(abs(nearest["mid"] - entry) / atr, 5.0), 4)
        out["fvg_fresh"] = (0.0 if nearest["inverted"]
                            else 0.5 if nearest["filled"] else 1.0)
        out["fvg_size_norm"] = round(
            min((nearest["top"] - nearest["bottom"]) / atr, 3.0), 4)
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
