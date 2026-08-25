"""Live market-signal dashboard for the EA's on-chart panel (2026-08-05).

WHAT THIS IS
------------
The right-hand column of the on-chart copier panel: HTF bias, the ICT
criteria breakdown, and a BUY/SELL confidence score. It is a READ-ONLY
view -- nothing here places, sizes, or blocks a trade. It exists so the
same evidence the reversal engine already computes is visible at the
terminal while watching price, instead of only in the app.

SPLIT OF WORK (deliberate)
--------------------------
The EA computes what is cheap and inherently local to the terminal:
bid/ask/spread/floating P/L, the M5-D1 trend row, ATR, session and
killzone countdown, VWAP. Those need a ticking clock and the chart's own
series; routing them through a socket would only add lag and a failure
mode.

This module computes what needs the ICT machinery that already exists in
forex_trader/reversal_engine/ict_patterns.py -- FVG, liquidity sweep,
displacement, order block -- plus the score that combines them. Porting
those to MQL5 would mean two implementations of the same patterns in two
languages, which is precisely how a chart and an app come to disagree
about what the market is doing.

The one deliberate duplication is the killzone window. The EA needs it
locally for the ticking "NEXT: NEW YORK in 03:21:13" countdown, and the
score here needs it as a boolean. Both read the same UTC windows defined
in KILLZONES below (the EA's copy is in PanelSession()); only the boolean
crosses the wire, so a drift between them can change a countdown label
but never the score.

Every failure returns a "no data" payload rather than raising. A panel
that goes blank for a cycle is a cosmetic problem; an exception on the
bridge's push path is not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.src.services.reversal_engine import ict_patterns as ict

log = logging.getLogger(__name__)

# UTC killzone windows, [start_hour, end_hour). Kept in lockstep with
# PanelSession() in mql5/ForexTraderBridge.mq5 -- see the module docstring
# for why the duplication is accepted and what it can and cannot affect.
KILLZONES = (
    ("ASIA",     0,  6),
    ("LONDON",   7, 10),
    ("NEW YORK", 12, 15),
)

# Points per criterion. These are a display weighting, not a risk model --
# nothing downstream reads them. They sum to 100 so the panel's "(60pts)"
# reads as a percentage without needing a second scale.
WEIGHTS = {
    "bias_align":  25,
    "fvg":         15,
    "sweep":       15,
    "displacement": 15,
    "killzone":    10,
    "order_block": 10,
    "vwap":        10,
}


def _grade(points: int) -> str:
    if points >= 75:
        return "A"
    if points >= 50:
        return "B"
    if points >= 30:
        return "C"
    return "D"


def in_killzone(now: Optional[datetime] = None) -> tuple[bool, str]:
    """(inside_a_killzone, name). Name is "" when outside all of them."""
    h = (now or datetime.now(timezone.utc)).hour
    for name, start, end in KILLZONES:
        if start <= h < end:
            return True, name
    return False, ""


def _htf_bias(h1: list[dict], h4: list[dict]) -> str:
    """BULLISH / BEARISH / NEUTRAL from H1 and H4 agreement.

    Reuses the reversal engine's own get_htf_bias when it is importable so
    the panel cannot disagree with the engine's level ranking; the simple
    close-vs-window fallback only runs if that import fails.
    """
    try:
        from backend.src.services.reversal_engine import level_detector as ld
        # get_htf_bias returns lowercase ('bullish'/'bearish'/'neutral').
        b = (ld.get_htf_bias(h1, h4) or "").upper()
        if b in ("BULLISH", "BEARISH", "NEUTRAL"):
            return b
    except Exception:
        pass
    if len(h1) < 10:
        return "NEUTRAL"
    closes = [ict._cl(c) for c in h1]
    ref = sum(closes[-20:-1]) / max(1, len(closes[-20:-1]))
    if closes[-1] > ref:
        return "BULLISH"
    if closes[-1] < ref:
        return "BEARISH"
    return "NEUTRAL"


def _has_displacement(candles: list[dict], recent_n: int = 3,
                      mult: float = 1.8) -> bool:
    """A recent candle whose body is `mult`x the average body of the prior
    20 -- the impulsive leg that separates a real ICT entry from drift."""
    if len(candles) < 25:
        return False
    bodies = [abs(ict._cl(c) - ict._op(c)) for c in candles[-25:-recent_n]]
    if not bodies:
        return False
    avg = sum(bodies) / len(bodies)
    if avg <= 0:
        return False
    return any(abs(ict._cl(c) - ict._op(c)) >= avg * mult
               for c in candles[-recent_n:])


def _active_fvg(candles: list[dict], price: float) -> tuple[bool, str]:
    """(any unfilled FVG left on this series, direction of the nearest one).

    "Active" means unfilled: a gap price has already traded back through
    has done its job and is no longer a magnet.
    """
    try:
        fvgs = [f for f in ict.detect_fvgs(candles) if not f.get("filled")]
    except Exception:
        return False, ""
    if not fvgs:
        return False, ""
    nearest = min(fvgs, key=lambda f: abs(
        (float(f.get("top", 0)) + float(f.get("bottom", 0))) / 2.0 - price))
    return True, str(nearest.get("direction") or "")


def _in_order_block(candles: list[dict], direction: str, price: float) -> bool:
    try:
        ob = ict.find_breaker_block(candles, direction)
    except Exception:
        return False
    if not ob:
        return False
    return float(ob["low"]) <= price <= float(ob["high"])


def _vwap(candles: list[dict]) -> Optional[float]:
    """Volume-weighted average price over the supplied candles.

    Falls back to a plain typical-price average when the feed reports no
    volume (some XAUUSD feeds report tick volume only, and a few report
    zero) -- an unweighted mean is still a usable mean-reversion reference,
    whereas returning None would blank two criteria for no good reason.
    """
    if not candles:
        return None
    num = den = 0.0
    plain: list[float] = []
    for c in candles:
        tp = (ict._hi(c) + ict._lo(c) + ict._cl(c)) / 3.0
        vol = float(c.get("tick_volume", c.get("volume", 0)) or 0)
        plain.append(tp)
        num += tp * vol
        den += vol
    if den > 0:
        return num / den
    return sum(plain) / len(plain)


def empty_payload(note: str = "NO DATA") -> dict:
    """The shape every caller gets, with everything off. Used verbatim when
    candles are unavailable, so the panel shows a clean "no data" rather
    than the previous cycle's stale numbers."""
    return {
        "bias": "NEUTRAL",
        "scanner": note,
        "headline": "",
        "buy_conf": 0, "sell_conf": 0,
        "buy_grade": "-", "sell_grade": "-",
        "fvg": False, "sweep": False, "displacement": False,
        "order_block": False, "killzone": False, "killzone_name": "",
        "vwap_buy_ok": False, "vwap_sell_ok": False,
        "bias_align": False,
        "levels": [],
    }


async def build_payload(bridge: Any) -> dict:
    """Compute one dashboard snapshot. Never raises."""
    try:
        m5  = await bridge.get_candles("M5", 120)
        m15 = await bridge.get_candles("M15", 120)
        h1  = await bridge.get_candles("H1", 50)
        h4  = await bridge.get_candles("H4", 12)
        tick = await bridge.get_tick()
    except Exception as e:
        log.debug("[Panel] candle fetch failed: %s", e)
        return empty_payload("NO FEED")

    if not m5 or not m15 or not h1 or not tick:
        return empty_payload("NO FEED")

    price = float(getattr(tick, "bid", 0) or 0)
    if price <= 0:
        return empty_payload("NO PRICE")

    bias = _htf_bias(h1, h4 or [])
    # Direction the criteria are evaluated FOR. A neutral HTF read still
    # scores both sides -- the panel shows a BUY and a SELL confidence
    # side by side precisely so a disagreement is visible.
    bull = bias == "BULLISH"

    fvg_on, fvg_dir = _active_fvg(m5, price)
    try:
        pools = ict.detect_equal_levels(m15)
        sweep = ict.detect_liquidity_sweep(m15, pools)
    except Exception:
        sweep = None
    disp = _has_displacement(m5)
    kz, kz_name = in_killzone()
    ob_bull = _in_order_block(m15, "bullish", price)
    ob_bear = _in_order_block(m15, "bearish", price)
    vw = _vwap(m5)
    vwap_buy_ok  = vw is not None and price >= vw
    vwap_sell_ok = vw is not None and price <= vw

    sweep_dir = (sweep or {}).get("reversal_direction") or ""

    def _score(side: str) -> int:
        want_bull = side == "BUY"
        pts = 0
        if (bias == "BULLISH") == want_bull and bias != "NEUTRAL":
            pts += WEIGHTS["bias_align"]
        if fvg_on and (not fvg_dir or (fvg_dir == "bullish") == want_bull):
            pts += WEIGHTS["fvg"]
        if sweep and (sweep_dir == "bullish") == want_bull:
            pts += WEIGHTS["sweep"]
        if disp:
            pts += WEIGHTS["displacement"]
        if kz:
            pts += WEIGHTS["killzone"]
        if (ob_bull if want_bull else ob_bear):
            pts += WEIGHTS["order_block"]
        if (vwap_buy_ok if want_bull else vwap_sell_ok):
            pts += WEIGHTS["vwap"]
        return pts

    buy_pts, sell_pts = _score("BUY"), _score("SELL")
    lead = "BUY" if buy_pts >= sell_pts else "SELL"
    lead_pts = max(buy_pts, sell_pts)

    return {
        "bias": bias,
        "scanner": "WAITING..." if lead_pts < 50 else f"{lead} SETUP FORMING",
        "headline": f"HTF: {lead} BIAS ({lead_pts}pts)",
        "buy_conf": buy_pts, "sell_conf": sell_pts,
        "buy_grade": _grade(buy_pts), "sell_grade": _grade(sell_pts),
        # The criteria row shows the LEADING side's reading, matching the
        # single Y/N column the panel has room for.
        "fvg": fvg_on,
        "sweep": bool(sweep),
        "displacement": disp,
        "order_block": ob_bull if lead == "BUY" else ob_bear,
        "killzone": kz, "killzone_name": kz_name,
        "vwap_buy_ok": vwap_buy_ok, "vwap_sell_ok": vwap_sell_ok,
        "bias_align": (bias == "BULLISH") == (lead == "BUY") and bias != "NEUTRAL",
        "levels": _levels(price),
    }


def _levels(price: float) -> list[dict]:
    """Top candidate levels for the panel's LEVELS tab, taken from the
    reversal engine's own cache rather than recomputed -- the panel must
    show what the engine would actually trade, not a second opinion.
    Empty list when the engine isn't running."""
    try:
        from backend.src.services.reversal_engine import reversal_engine_service as res
        eng = res.get_instance()
        if eng is None:
            return []
        # _run_cycle stores get_candidate_levels()'s output under
        # cached["levels"] -- each entry carries price/type/score/direction.
        cands = ((eng.get_status() or {}).get("cached") or {}).get("levels") or []
    except Exception:
        return []
    out = []
    for c in cands[:6]:
        try:
            px = float(c.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out.append({
            "price": round(px, 2),
            "kind": str(c.get("type") or ""),
            "dir": str(c.get("direction") or ""),
            "dist": round(px - price, 2),
        })
    return out
