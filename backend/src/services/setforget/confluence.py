"""The confluence checklist -- the scored list the G-Club community keeps.

Its whole value is that it says no. A checklist that scores everything highly
is worse than no checklist, because it launders a coin flip into a number the
operator will trust. Two design decisions follow from that:

**The weights are not cosmetic.** Higher-timeframe agreement and being at an
area of interest carry two points each because in Alex G's material they are
the entry CONDITIONS; everything else is a reason to like an entry that already
qualifies. Flat weights would let four weak confirmations outvote the two items
that decide whether there is a trade at all.

**Missing evidence never scores.** A model that cannot see the weekly chart has
not confirmed the weekly chart. An item that passes because its field was None
would make the score rise as the evidence got worse, and nothing on screen
would look wrong.

The checklist scores; it does not decide. `analysis.py` is what refuses, and
`setup.invalidations` is what refuses on the numbers.
"""
from __future__ import annotations

from typing import Optional

# The retracement band a pullback has to land in. Under 38.2% price has barely
# pulled back and the zone is not in play; over 78.6% the leg is most of the
# way undone and the structure that justified the trade is going with it.
FIB_LOW = 0.382
FIB_HIGH = 0.786

# The levels the chart draws. The first and last ARE the band's edges, so the
# picture and the score cannot disagree about where the zone is -- a band drawn
# from one set of numbers and scored against another would show price inside
# the zone while the checklist said it was outside, with nothing on screen
# saying which was right. 50% and 61.8% sit between them because those are the
# two a pullback is judged against; they are drawn, not scored.
LEVELS: tuple[float, ...] = (FIB_LOW, 0.5, 0.618, FIB_HIGH)

# Momentum is a veto, not a trigger: what disqualifies a long is buying into an
# exhausted push, not the absence of a strong one.
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

# Of the weighted total, not the item count.
HIGH_PCT = 75.0
MODERATE_PCT = 50.0

_MISSING = "Not available — this was not scored."


def _bias_for(direction: str) -> str:
    return "bullish" if str(direction).upper() == "BUY" else "bearish"


def _htf(ev: dict, want: str) -> tuple[bool, str]:
    weekly, daily = ev.get("weekly_bias"), ev.get("daily_bias")
    if not weekly or not daily:
        return False, _MISSING
    if weekly != daily:
        return False, (f"Weekly is {weekly} and Daily is {daily}. They disagree, "
                       f"so the pair is too noisy to trade.")
    if weekly != want:
        return False, (f"Weekly and Daily agree, and they are {weekly} — "
                       f"against this trade.")
    return True, f"Weekly and Daily are both {weekly}, with the trade."


def _structure(ev: dict, want: str) -> tuple[bool, str]:
    entry = ev.get("entry_bias")
    if not entry:
        return False, _MISSING
    if entry != want:
        return False, f"Entry timeframe structure is {entry}, against the trade."
    return True, f"Entry timeframe is making {'higher' if want == 'bullish' else 'lower'} " \
                 f"highs and lows, with the trade."


def _at_aoi(ev: dict, want: str) -> tuple[bool, str]:
    zone = ev.get("at_zone")
    if not zone:
        return False, "Price is not at an area of interest."
    want_kind = "demand" if want == "bullish" else "supply"
    if zone.get("kind") != want_kind:
        return False, (f"Price is at a {zone.get('kind')} zone. That is the wrong "
                       f"side of the market for this trade.")
    touches = int(zone.get("touches") or 1)
    return True, (f"Price is at a {want_kind} zone "
                  f"{zone.get('low'):.2f}–{zone.get('high'):.2f}"
                  + (f", tested {touches} times." if touches > 1 else "."))


def _ema_trend(ev: dict, want: str) -> tuple[bool, str]:
    fast, slow = ev.get("ema_fast"), ev.get("ema_slow")
    if fast is None or slow is None:
        return False, _MISSING
    if (fast > slow) == (want == "bullish"):
        return True, (f"EMA 50 is {'above' if fast > slow else 'below'} EMA 200 "
                      f"({fast:.2f} vs {slow:.2f}), with the trade.")
    return False, f"EMA 50 {fast:.2f} vs EMA 200 {slow:.2f} is against the trade."


def _fib_zone(ev: dict, _want: str) -> tuple[bool, str]:
    fib = ev.get("fib")
    if fib is None:
        return False, _MISSING
    if FIB_LOW <= fib <= FIB_HIGH:
        return True, f"Pullback is {fib * 100:.1f}% of the last leg — inside the " \
                     f"{FIB_LOW * 100:.1f}–{FIB_HIGH * 100:.1f}% band."
    if fib < FIB_LOW:
        return False, f"Pullback is only {fib * 100:.1f}% of the last leg — too " \
                      f"shallow to have reached the zone."
    return False, f"Pullback is {fib * 100:.1f}% of the last leg — the move is " \
                  f"most of the way undone."


def _momentum(ev: dict, want: str) -> tuple[bool, str]:
    rsi = ev.get("rsi")
    if rsi is None:
        return False, _MISSING
    if want == "bullish" and rsi >= RSI_OVERBOUGHT:
        return False, f"RSI {rsi:.1f} is overbought — this would be buying an " \
                      f"exhausted push."
    if want == "bearish" and rsi <= RSI_OVERSOLD:
        return False, f"RSI {rsi:.1f} is oversold — this would be selling an " \
                      f"exhausted push."
    return True, f"RSI {rsi:.1f} leaves room in the direction of the trade."


def _confirmation(ev: dict, want: str) -> tuple[bool, str]:
    found = ev.get("confirmation")
    if not found:
        return False, "No confirmation candle on the last closed bar."
    if found.get("direction") != want:
        return False, (f"The last closed bar is a {found.get('direction')} "
                       f"{str(found.get('kind', '')).replace('_', ' ')} — the wrong way.")
    return True, (f"The last closed bar is a {want} "
                  f"{str(found.get('kind', '')).replace('_', ' ')}.")


# id, label, weight, check. Data rather than a chain of ifs: adding an item is
# one row, and no caller can score a list that differs from the one rendered.
CHECKS: tuple[tuple[str, str, int, object], ...] = (
    ("htf_agreement", "Weekly and Daily agree", 2, _htf),
    ("at_aoi", "Price is at an area of interest", 2, _at_aoi),
    ("structure", "Entry timeframe structure agrees", 1, _structure),
    ("ema_trend", "EMA 50 / 200 trend agrees", 1, _ema_trend),
    ("fib_zone", "Pullback is in the Fibonacci band", 1, _fib_zone),
    ("momentum", "RSI is not exhausted against the trade", 1, _momentum),
    ("confirmation", "A confirmation candle has closed", 1, _confirmation),
)

MAX_SCORE = sum(weight for _, _, weight, _ in CHECKS)


def score(evidence: dict) -> dict:
    """Score the checklist. Returns items, the weighted total, and a grade.

    `pct` is of the weighted total, never of the item count: four of seven
    items is not 57% when the three that failed are the heavy ones, and a
    count-based percentage would flatter exactly the setups the weights exist
    to catch.
    """
    want = _bias_for(evidence.get("direction", "BUY"))
    items = []
    total = 0
    for item_id, label, weight, check in CHECKS:
        passed, detail = check(evidence, want)          # type: ignore[operator]
        if passed:
            total += weight
        items.append({"id": item_id, "label": label, "weight": weight,
                      "passed": passed, "detail": detail})

    pct = total / MAX_SCORE * 100 if MAX_SCORE else 0.0
    return {"items": items, "score": total, "max": MAX_SCORE,
            "pct": pct, "grade": grade(pct)}


def grade(pct: float) -> str:
    if pct >= HIGH_PCT:
        return "high"
    if pct >= MODERATE_PCT:
        return "moderate"
    return "low"


def retracement(impulse: Optional[dict], price: float) -> Optional[float]:
    """How far price has pulled back into a completed leg, as 0..1.

    0 is "still at the end of the leg" and 1 is "the whole leg is undone".
    None when there is no completed leg or it had no height -- a ratio measured
    over a zero-length move is a division by zero wearing a percentage sign.
    """
    if not impulse:
        return None
    start, end = float(impulse["start"]), float(impulse["end"])
    span = end - start
    if span == 0:
        return None
    return (end - price) / span


def retracement_price(impulse: Optional[dict], ratio: float) -> Optional[float]:
    """Where a given pullback into a completed leg sits, in price.

    The exact inverse of `retracement`, and deliberately written as one: the
    chart draws from this and the checklist scores from that, so a second
    derivation would be a second answer to where 61.8% is.

    Works in both directions. A short's impulse runs high to low, so its span
    is negative and its 61.8% sits ABOVE the end of the leg -- a formula that
    only knew how to go down would draw every short's band on the wrong side
    of price.
    """
    if not impulse:
        return None
    start, end = float(impulse["start"]), float(impulse["end"])
    span = end - start
    if span == 0:
        return None
    return end - ratio * span
