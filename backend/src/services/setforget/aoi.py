"""Areas of interest -- the zones the method trades at, and nowhere else.

An AOI is a support or resistance band price is likely to react at: in Alex G's
material a former swing point or a consolidation, which is a supply/demand zone
by another name. Entries only happen at one, in line with the higher-timeframe
bias, so this file decides where a trade is even allowed to exist.

**Width is the whole argument.** A zone drawn too wide swallows the current
price, every candidate then reads as "at a zone", the checklist scores it, and
the method has quietly become "trade whenever". So a zone is derived from the
swing candle's own rejection wick -- the distance between where price went and
where it finished -- rather than from a percentage of anything.

`touches` is how many swing points merged into a level. A band price has turned
at twice is a stronger area than one it turned at once, and that is the only
thing the number claims.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.setforget import structure

# Beyond this many zones the chart is a wall of boxes and the page is useless.
# The nearest ones to price are the ones a setup can be built on, so that is
# what survives the trim.
DEFAULT_LIMIT = 8


def zones(candles: list[dict], lookback: int = structure.DEFAULT_LOOKBACK,
          limit: int = DEFAULT_LIMIT, reference: Optional[float] = None,
          gap: float = 0.0) -> list[dict]:
    """Supply and demand zones from the confirmed swing points, in price order.

    Each is `{"kind", "low", "high", "ts", "touches"}` with kind "demand" below
    price or "supply" above it. `reference` is the price the trim is measured
    from; it defaults to the last close. `gap` is how far apart two bands can
    be and still be one level -- see `merge`.
    """
    if not candles:
        return []

    raw: list[dict] = []
    for point in structure.swing_points(candles, lookback):
        c = candles[point["idx"]]
        o, close = float(c.get("open") or 0.0), float(c.get("close") or 0.0)
        if point["kind"] == "low":
            low, high = point["price"], min(o, close)
            # No rejection wick: the body itself is the zone. Without this the
            # band would be zero-height, price would never be "in" it, and the
            # page would show an untradeable level that looks entirely normal.
            if high <= low:
                high = max(o, close)
            kind = "demand"
        else:
            low, high = max(o, close), point["price"]
            if high <= low:
                low = min(o, close)
            kind = "supply"
        if high <= low:                       # a candle with no range at all
            continue
        raw.append({"kind": kind, "low": low, "high": high,
                    "ts": point["ts"], "touches": 1})

    merged = merge(raw, gap=gap)
    if len(merged) <= limit:
        return merged

    price = reference if reference is not None else float(candles[-1].get("close") or 0.0)
    nearest = sorted(merged, key=lambda z: distance(z, price))[:limit]
    return sorted(nearest, key=lambda z: z["low"])


def merge(raw: list[dict], gap: float = 0.0) -> list[dict]:
    """Fold nearby zones of the same kind into one, in price order.

    Two swing lows a tick apart are one area of interest. Left separate they
    would each score the checklist and the same level would be counted twice --
    which is how a single support band ends up reading as strong confluence.

    `gap` extends that from "overlapping" to "within this far of each other",
    and it is the difference between a usable chart and an unusable one. Over a
    400-bar window the detector finds a dozen bands stacked within a few points
    of each other; the next opposing zone is then always a point or two from
    the entry, and EVERY candidate comes out under 1:2 and is refused. A trader
    drawing that same chart draws four or five chunky levels. The caller
    supplies the gap because what counts as "nearby" is how far the instrument
    moves in a bar, not a number this module should pick.

    Merging is transitive: three bands each within `gap` of the next are one
    level, because the sweep compares against the band it is BUILDING rather
    than against the original it started from.

    The merged band keeps the MOST RECENT timestamp: it is one level, and the
    age shown beside it should be the last time price was there.
    """
    out: list[dict] = []
    for kind in ("demand", "supply"):
        group = sorted([z for z in raw if z["kind"] == kind], key=lambda z: z["low"])
        for zone in group:
            if out and out[-1]["kind"] == kind and zone["low"] <= out[-1]["high"] + gap:
                last = out[-1]
                last["high"] = max(last["high"], zone["high"])
                last["ts"] = max(last["ts"], zone["ts"])
                last["touches"] += zone["touches"]
            else:
                out.append(dict(zone))
    return sorted(out, key=lambda z: z["low"])


def distance(zone: dict, price: float) -> float:
    """How far price is from the band. Zero while it is inside."""
    if zone["low"] <= price <= zone["high"]:
        return 0.0
    return min(abs(price - zone["low"]), abs(price - zone["high"]))


def at_price(zones_: list[dict], price: float, kind: str,
             tolerance: float = 0.0) -> Optional[dict]:
    """The nearest zone of `kind` that price is in, or within `tolerance` of.

    Price rarely touches a hand-drawn level to the tick, so a tolerance of zero
    would mean the setup exists only on the bar that happens to print inside
    the band. The caller supplies it because what counts as close depends on
    the instrument and the timeframe, not on this function.
    """
    candidates = [z for z in zones_ if z["kind"] == kind
                  and distance(z, price) <= tolerance]
    if not candidates:
        return None
    return min(candidates, key=lambda z: distance(z, price))


def next_opposing(zones_: list[dict], price: float, direction: str) -> Optional[dict]:
    """Where the trade takes profit: the next zone price would run INTO.

    A long targets the nearest supply above, a short the nearest demand below.
    A zone on the wrong side of price is not a target -- returning one would
    put the take-profit past the entry in the wrong direction and invert the
    trade, which reads as a perfectly plausible number on screen.
    """
    if direction.upper() == "BUY":
        above = [z for z in zones_ if z["kind"] == "supply" and z["low"] > price]
        return min(above, key=lambda z: z["low"]) if above else None
    below = [z for z in zones_ if z["kind"] == "demand" and z["high"] < price]
    return max(below, key=lambda z: z["high"]) if below else None
