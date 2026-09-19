"""Candle builders for the Set & Forget tests.

Every test in this package needs a series with a KNOWN shape -- a confirmed
higher high, a pin bar at a demand zone, a 61.8% pullback. Building those by
hand inside each test is how two tests end up asserting against series that
differ in a way neither of them meant.

`series` takes the closes and derives plausible wicks; `zigzag` builds a
trending series whose swing points are at indices the caller chose. Neither
invents randomness: a test that cannot be read off the literal it was given is
a test nobody will trust when it goes red.
"""
from __future__ import annotations

# An arbitrary but fixed start: 2026-01-01 00:00 UTC. Gold's real timestamps
# are irrelevant to the maths and a fixed base keeps failures readable.
BASE_TS = 1767225600.0
HOUR = 3600.0


def candle(ts: float, o: float, h: float, low: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": low, "close": c}


def series(closes: list[float], *, wick: float = 1.0, step: float = HOUR) -> list[dict]:
    """A series whose closes are exactly `closes`.

    Each candle opens at the previous close, and its high/low extend `wick`
    beyond the body. That makes highs and lows track the closes, so a swing in
    the close series is a swing in the high/low series too -- which is what the
    structure tests want to talk about.
    """
    out: list[dict] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(candle(
            BASE_TS + i * step,
            o,
            max(o, c) + wick,
            min(o, c) - wick,
            c,
        ))
        prev = c
    return out


def zigzag(legs: list[tuple[float, int]], *, wick: float = 1.0) -> list[dict]:
    """A series that walks to each (price, bars) leg in a straight line.

    `zigzag([(100, 1), (120, 10), (110, 5)])` starts at 100, rises to 120 over
    ten bars, then falls to 110 over five. The turning points land on known
    indices, which is what makes a swing-point assertion meaningful.
    """
    closes: list[float] = [legs[0][0]]
    for price, bars in legs[1:]:
        start = closes[-1]
        for n in range(1, bars + 1):
            closes.append(start + (price - start) * n / bars)
    return series(closes, wick=wick)
