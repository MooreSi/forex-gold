"""Market structure: swing points, the bias they spell, and the last impulse.

The bottom of the Set & Forget stack. Areas of interest are built from swing
points, the confluence checklist scores against the bias, and the Fibonacci
band is measured over the impulse -- so everything above this file inherits
whatever this file gets wrong, silently.

Two decisions are load-bearing and neither is arbitrary:

**A swing is confirmed or it does not exist.** The highest bar of a rising
series is the last one, and it is not a swing high -- nothing has turned back
down from it yet. Counting it would let a trade be taken against "structure"
that is really just the right-hand edge of the chart, which is the single most
common way a top-down read talks itself into a top.

**Ties resolve to the earlier bar.** Real candles rarely tie, but a bar that
tops out and the bar that opens at its close often share a high to the tick,
and gold quotes to two decimals. Without a rule, one series gives two swings
where a trader sees one. The rule: strictly higher than everything before it
in the window, at least as high as everything after.
"""
from __future__ import annotations

from typing import Optional

# Two bars either side. Enough to reject single-bar noise, small enough that a
# 4H swing is confirmed within a working day rather than a week.
DEFAULT_LOOKBACK = 2

# How many confirmed points of each kind a bias needs. One higher high is not
# an uptrend; a higher high AND a higher low is, and that takes four points.
_POINTS_FOR_BIAS = 2


def swing_points(candles: list[dict], lookback: int = DEFAULT_LOOKBACK) -> list[dict]:
    """Confirmed swing highs and lows, oldest first.

    Each point is `{"idx", "ts", "price", "kind"}` where kind is "high" or
    "low" and price is the wick extreme -- the high of a swing high, the low of
    a swing low. The wick is the point: it is where stops sit, and an area of
    interest drawn to the body would leave them outside the zone.
    """
    if lookback < 1 or len(candles) < lookback * 2 + 1:
        return []

    out: list[dict] = []
    for i in range(lookback, len(candles) - lookback):
        before = candles[i - lookback:i]
        after = candles[i + 1:i + 1 + lookback]
        high = float(candles[i].get("high") or 0.0)
        low = float(candles[i].get("low") or 0.0)

        if (all(high > float(c.get("high") or 0.0) for c in before)
                and all(high >= float(c.get("high") or 0.0) for c in after)):
            out.append({"idx": i, "ts": float(candles[i].get("ts") or 0.0),
                        "price": high, "kind": "high"})
        elif (all(low < float(c.get("low") or 0.0) for c in before)
                and all(low <= float(c.get("low") or 0.0) for c in after)):
            out.append({"idx": i, "ts": float(candles[i].get("ts") or 0.0),
                        "price": low, "kind": "low"})
    return out


def bias(candles: list[dict], lookback: int = DEFAULT_LOOKBACK) -> str:
    """"bullish", "bearish", "ranging" or "unknown".

    "ranging" and "unknown" are different answers and the page shows which.
    Ranging means the structure was read and it disagrees with itself -- an
    expanding range, both sides moving, exactly the pair Alex G says to skip.
    Unknown means there was not enough confirmed chart to read at all. Neither
    may carry a setup, but only one of them is worth waiting out.
    """
    points = swing_points(candles, lookback)
    highs = [p["price"] for p in points if p["kind"] == "high"]
    lows = [p["price"] for p in points if p["kind"] == "low"]
    if len(highs) < _POINTS_FOR_BIAS or len(lows) < _POINTS_FOR_BIAS:
        return "unknown"

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]
    if higher_high and higher_low:
        return "bullish"
    if not higher_high and not higher_low:
        return "bearish"
    return "ranging"


def last_impulse(candles: list[dict], direction: str,
                 lookback: int = DEFAULT_LOOKBACK) -> Optional[dict]:
    """The most recent completed leg, as `{"start", "end", "start_ts", "end_ts"}`.

    What a Fibonacci retracement is drawn over. In an uptrend it runs from the
    swing low up to the swing high that followed it, so `start < end`; in a
    downtrend it is the mirror. `None` when no leg has completed, which is the
    honest answer on a chart that has only ever gone one way.
    """
    points = swing_points(candles, lookback)
    want_end = "high" if direction == "bullish" else "low"
    want_start = "low" if direction == "bullish" else "high"

    end = next((p for p in reversed(points) if p["kind"] == want_end), None)
    if end is None:
        return None
    start = next((p for p in reversed(points)
                  if p["kind"] == want_start and p["idx"] < end["idx"]), None)
    if start is None:
        return None
    return {"start": start["price"], "end": end["price"],
            "start_ts": start["ts"], "end_ts": end["ts"]}


# How many bars back a shift may have happened and still count as a trigger.
# A shift twenty bars ago is history: reading it as live is how an entry fires
# on a move that already happened and is over.
DEFAULT_SHIFT_WINDOW = 3


def shift_of_structure(candles: list[dict], direction: str,
                       within: int = DEFAULT_SHIFT_WINDOW,
                       lookback: int = DEFAULT_LOOKBACK) -> Optional[dict]:
    """The 30-minute trigger: price CLOSING through the last swing against it.

    The higher timeframes say where a trade belongs and which way. This says
    when. Alex G's guide puts the entry on the lowest of three timeframes --
    "a 4:1 or 8:1 ratio (e.g. Daily, 4H, 30-min)" -- and the community's
    checklist gives Shift of Structure the heaviest weight in its 2H/1H/30m
    group. For a long that is a close above the most recent confirmed swing
    high, after the run of lower highs that carried price into the zone: the
    first evidence the sellers who drove it there have stopped.

    **The close, never the wick.** A wick through a level is a test of it; only
    a close says it changed hands. Without that distinction every spike into a
    level is an entry, and spikes into levels are exactly what a good level
    produces.

    Returns `{"idx", "ts", "level", "direction"}` -- `level` is the swing that
    was broken, which is what a pending entry would be placed against. A
    detector that only answered True would leave that price to be guessed.
    """
    direction = str(direction or "").strip().upper()
    if len(candles) < lookback * 2 + 2:
        return None

    want = "high" if direction == "BUY" else "low"
    points = [p for p in swing_points(candles, lookback) if p["kind"] == want]
    if not points:
        return None

    first = max(len(candles) - within, 0)
    for idx in range(len(candles) - 1, first - 1, -1):
        close = float(candles[idx].get("close") or 0.0)
        # The swing must PREDATE the bar that broke it, or a bar is being
        # compared against a level it defined itself.
        prior = [p for p in points if p["idx"] < idx]
        if not prior:
            continue
        level = prior[-1]["price"]
        previous = float(candles[idx - 1].get("close") or 0.0) if idx else close
        # The bar that CROSSED, not every bar after it. Price sitting above a
        # level it broke twenty bars ago is not a fresh trigger -- without this
        # the recency window means nothing, because the most recent bar always
        # satisfies "close is above the level" for as long as the move lasts.
        broke = (close > level >= previous) if direction == "BUY" \
            else (close < level <= previous)
        if broke:
            return {"idx": idx, "ts": float(candles[idx].get("ts") or 0.0),
                    "level": float(level), "direction": direction}
    return None
