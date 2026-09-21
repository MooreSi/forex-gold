"""Areas of interest -- the zones the method trades at, and nowhere else.

An AOI is a support or resistance band price is likely to react at: in Alex G's
material a former swing point or a consolidation, which is a supply/demand zone
by another name. Entries only happen at one, in line with the higher-timeframe
bias, so this file decides where a trade is even allowed to exist.

**Width is the whole argument.** A zone drawn too wide swallows the current
price, every candidate then reads as "at a zone", the checklist scores it, and
the method has quietly become "trade whenever".

So a zone spans the swing candle's BODY. Alex G's guide says it twice -- mark
structure "using candle-body closes -- ignoring long wicks", and draw the zone
"focusing on candle bodies (not extended wicks)" -- and the reason it gives is
that bodies show where most traders actually closed. Until 2026-09-21 this file
drew from the wick extreme to the body, which made every band as tall as the
rejection that formed it: a long tail is exactly what marks a good level, and
it was producing the widest, most swallowing zones.

The STOP still measures from the wick, because the guide puts it "just below
the pin bar's tail". Bodies decide where the zone is; wicks decide where the
trade is wrong. Different questions, different answers -- see
`analysis._stop_for`.

`touches` is how many swing points merged into a level, and since 2026-09-21 it
is a gate rather than a hint: a band is not an area of interest at all until
price has turned there `MIN_TOUCHES` times. `mark` is the entry point -- it
scans backward only as far as it must to validate the levels either side of
price, and stops.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.setforget import structure

# Beyond this many zones the chart is a wall of boxes and the page is useless.
# The nearest ones to price are the ones a setup can be built on, so that is
# what survives the trim.
DEFAULT_LIMIT = 8

# How many times price must have turned at a band before it is an area of
# interest at all. The owner's rule, 2026-09-21: three touches validate a key
# horizontal zone, and nothing under that is a level. Before it, one swing
# point was a tradeable zone -- which is how a single wick anywhere on the
# chart became an entry.
MIN_TOUCHES = 3

# How wide a band may be and still be one level, as a fraction of price.
#
# Alex G's guide: "Keep the zone reasonably narrow (avoid overly wide zones --
# aim for ~<60 pips)". A band wider than that is not a level: price sits inside
# it most of the time, so every read comes back "at an area of interest" and
# the checklist scores its heaviest item on nothing.
#
# A FRACTION of price, not a pip count. The guide's figure is written for FX
# majors and this app trades gold, where "a pip" is $0.01, $0.10 or $1.00
# depending on who is speaking -- so a literal translation would be false
# precision. Sixty pips on a major at ~1.08 is about 0.55% of price.
#
# This is deliberately looser than that, at 1.5%, and it is a RAIL rather than
# the guide's number: its job is to catch a band wide enough to swallow price,
# which is the failure it exists for -- the 1035-point band measured on
# 2026-09-21 was 24% of price, so this stops it by a factor of sixteen.
# Tightening it to the literal 0.55% is a decision for the owner once he says
# what "60 pips" means on his own gold charts:
# docs/simon-handover/041-gold-pip-for-the-aoi-width-cap.md.
MAX_ZONE_WIDTH_PCT = 0.015

# The backward scan starts here and grows by SCAN_STEP until the zones either
# side of price are found. Below the start there are not enough confirmed
# swings for a third touch to exist, so testing smaller windows only costs
# passes that cannot succeed.
SCAN_START = 40
SCAN_STEP = 10


def zones(candles: list[dict], lookback: int = structure.DEFAULT_LOOKBACK,
          limit: int = DEFAULT_LIMIT, reference: Optional[float] = None,
          gap: float = 0.0, min_touches: int = 1) -> list[dict]:
    """Supply and demand zones from the confirmed swing points, in price order.

    Each is `{"kind", "low", "high", "ts", "touches"}` with kind "demand" below
    price or "supply" above it. `reference` is the price the trim is measured
    from; it defaults to the last close. `gap` is how far apart two bands can
    be and still be one level -- see `merge`.

    `min_touches` is the validation gate. It is applied BEFORE the nearest-N
    trim, never after: a trim that ran first could drop a validated level in
    favour of a nearer unvalidated one, and the page would show a zone that no
    rule had ever passed with nothing to distinguish it.
    """
    if not candles:
        return []

    raw: list[dict] = []
    for point in structure.swing_points(candles, lookback):
        c = candles[point["idx"]]
        o, close = float(c.get("open") or 0.0), float(c.get("close") or 0.0)
        # The BODY, both edges. Not the wick -- see the module docstring.
        low, high = min(o, close), max(o, close)
        kind = "demand" if point["kind"] == "low" else "supply"
        if high <= low:
            # A doji: open and close to the tick, so the body has no height.
            # Price could never be "inside" such a band, so it would render as
            # a level nobody can trade rather than as no level at all.
            continue
        raw.append({"kind": kind, "low": low, "high": high,
                    "ts": point["ts"], "touches": 1})

    merged = merge(raw, gap=gap)
    if min_touches > 1:
        merged = [z for z in merged if z["touches"] >= min_touches]
    if len(merged) <= limit:
        return merged

    price = reference if reference is not None else float(candles[-1].get("close") or 0.0)
    nearest = sorted(merged, key=lambda z: distance(z, price))[:limit]
    return sorted(nearest, key=lambda z: z["low"])


def mark(candles: list[dict], price: float,
         lookback: int = structure.DEFAULT_LOOKBACK,
         min_touches: int = MIN_TOUCHES,
         limit: int = DEFAULT_LIMIT) -> tuple[list[dict], int]:
    """The major levels, found by scanning BACKWARD until they are, and no
    further. Returns `(zones, bars_scanned)`.

    The objective is not to look back endlessly. The scan grows a window from
    the most recent bar until there is a validated zone on each side of price
    -- one to enter at and one to take profit at, which is the same pair in
    either direction -- and then stops. Everything older is history the method
    does not trade.

    Growing the window is what implements "the MOST RECENT three touches"
    rather than "any three touches ever". A band's touch count accumulates as
    the window reaches further back, so a level becomes validated exactly when
    the window first contains its third touch, and the scan halts on the first
    window that carries the pair.

    Stopping early is also what keeps the bands narrow. Over a long enough
    window a trending instrument tiles its whole range with swing bands, they
    overlap, and they fold into one level wide enough to swallow the current
    price -- measured on 2026-09-21 as a 1035-point "zone" with 80 touches
    that turned the method into "trade whenever". See the domain file.

    `bars_scanned` is returned rather than logged because the page has to be
    able to say how much history the levels came from. "Does it really measure
    back to last September?" was a real question with no answer on screen.

    No ATR is used anywhere here. Bands are one level when they overlap, which
    is the owner's rule: an ATR proximity gap glues distinct levels together
    and manufactures the very touch counts the validation is reading.
    """
    if not candles:
        return [], 0

    widths = list(range(SCAN_START, len(candles), SCAN_STEP)) + [len(candles)]
    found: list[dict] = []
    for width in widths:
        found = zones(candles[-width:], lookback=lookback, limit=limit,
                      reference=price, gap=0.0, min_touches=min_touches)
        pair = _nearest_pair(found, price)
        if len(pair) == 2:
            return pair, width
    # Never completed a pair, so the scan ran to the end of the series. Only
    # the nearest validated band on each side survives even so -- returning
    # everything the scan passed on the way IS the endless lookback the rule
    # exists to stop. Measured live on 2026-09-21: the weekly had no validated
    # supply above price, ran the full 82 bars, and handed back five levels,
    # two of them from July and August 2025.
    #
    # A missing side stays missing. It is never topped up with the best
    # unvalidated band, which is exactly what the three-touch rule refuses --
    # `propose` then says which side was missing, which is the useful answer.
    return _nearest_pair(found, price), len(candles)


def _nearest_pair(found: list[dict], price: float) -> list[dict]:
    """The nearest validated demand at or below price, and the nearest
    validated supply at or above it, in price order.

    That pair is what a setup is built from in EITHER direction: a long enters
    at the demand and targets the supply, a short does the reverse. Kind and
    side are checked together because a supply band below price is not the
    supply a long can take profit at, however near it happens to be.
    """
    below = [z for z in found if z["kind"] == "demand" and z["low"] <= price]
    above = [z for z in found if z["kind"] == "supply" and z["high"] >= price]
    out = []
    if below:
        out.append(min(below, key=lambda z: distance(z, price)))
    if above:
        out.append(min(above, key=lambda z: distance(z, price)))
    return sorted(out, key=lambda z: z["low"])


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


def within_width(zones_: list[dict], price: float) -> list[dict]:
    """Drop the bands too wide to be one level. See `MAX_ZONE_WIDTH_PCT`.

    A price of zero -- an unreadable tick -- drops nothing. There is no
    percentage to size against, and refusing every level would render as a
    market with no structure in it rather than as missing data.
    """
    if price <= 0:
        return list(zones_)
    cap = price * MAX_ZONE_WIDTH_PCT
    return [z for z in zones_ if (z["high"] - z["low"]) <= cap]


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
