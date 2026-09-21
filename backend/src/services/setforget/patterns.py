"""Confirmation candles: engulfing bars and pin bars.

Price reaching an area of interest is not a setup. Price reaching it and being
pushed back out is. That push is what this module looks for, and it does two
jobs at once -- it decides whether the setup exists, and it reports the tail
the stop goes beyond. Alex G's stop is below the tail of the confirmation
candle, so a detector that fired on the wrong bar would also place the stop on
the wrong bar, and the second error is the expensive one.

The thresholds below are the conventional price-action ones rather than
anything proprietary. They are module constants so a future disagreement about
what counts as "a small body" is one edit in one place with one set of tests
around it, not a number copied into three callers.
"""
from __future__ import annotations

from typing import Optional

# A pin bar's body may be at most a third of its range: more than that and the
# bar is a trend bar with a wick, not a rejection.
MAX_BODY_FRACTION = 0.33
# The rejecting tail must be at least half the range...
MIN_TAIL_FRACTION = 0.50
# ...and the tail on the other side at most a fifth, or the bar rejected both
# ends, which means it rejected neither and is a doji.
MAX_OPPOSITE_TAIL_FRACTION = 0.20

# A doji's body, at most, as a fraction of its range. Alex G's guide calls it
# "open and close are nearly equal"; measured against the bar's OWN range
# because a $2 body is indecision on a $40 gold bar and a trend bar on a $3 one.
MAX_DOJI_BODY_FRACTION = 0.08


def _body(c: dict) -> tuple[float, float]:
    o, cl = float(c.get("open") or 0.0), float(c.get("close") or 0.0)
    return (min(o, cl), max(o, cl))


def engulfing(previous: dict, current: dict) -> Optional[str]:
    """"bullish", "bearish" or None for the two-bar engulfing pattern.

    Bodies, not ranges. A bar whose wicks span the previous bar has not
    engulfed it: the wick is where price went and came back from, and the body
    is where it finished. A doji is excluded explicitly rather than by
    threshold -- a bar that opens and closes at the same price has no body to
    engulf with, however wide its range.
    """
    p_open, p_close = float(previous.get("open") or 0.0), float(previous.get("close") or 0.0)
    c_open, c_close = float(current.get("open") or 0.0), float(current.get("close") or 0.0)
    p_low, p_high = _body(previous)
    c_low, c_high = _body(current)

    covers = c_low <= p_low and c_high >= p_high
    if not covers:
        return None
    if c_close > c_open and p_close < p_open:
        return "bullish"
    if c_close < c_open and p_close > p_open:
        return "bearish"
    return None


def pin_bar(c: dict) -> Optional[str]:
    """"bullish", "bearish" or None for a single-bar rejection.

    Bullish is a long lower tail -- price was offered lower and refused -- and
    bearish is the mirror. A zero-range bar answers None rather than dividing
    by it.
    """
    high, low = float(c.get("high") or 0.0), float(c.get("low") or 0.0)
    rng = high - low
    if rng <= 0:
        return None
    body_low, body_high = _body(c)
    if (body_high - body_low) / rng > MAX_BODY_FRACTION:
        return None

    upper = (high - body_high) / rng
    lower = (body_low - low) / rng
    if lower >= MIN_TAIL_FRACTION and upper <= MAX_OPPOSITE_TAIL_FRACTION:
        return "bullish"
    if upper >= MIN_TAIL_FRACTION and lower <= MAX_OPPOSITE_TAIL_FRACTION:
        return "bearish"
    return None


def inside_bar(previous: dict, current: dict) -> bool:
    """Whether `current`'s whole RANGE sits inside `previous`'s.

    The guide: "a SMALLER candle whose range is completely within the previous
    candle's high-low". Both halves are load-bearing.

    Range, not body -- a bar whose body is contained but whose wick pokes past
    the prior high has broken that high, which is the opposite signal to a
    quiet pause.

    Smaller, not equal -- a bar with the same high and low as the one before it
    has not contracted, and a run of identical bars is a dead feed rather than
    a series of inside bars. That distinction is the difference between this
    returning None on a flat chart and it reporting a pattern on every bar.

    It carries no direction of its own. An inside bar is a pause, and which way
    it resolves is the trend's business, so callers read it as continuation in
    the direction already in force.
    """
    p_high, p_low = float(previous.get("high") or 0.0), float(previous.get("low") or 0.0)
    c_high, c_low = float(current.get("high") or 0.0), float(current.get("low") or 0.0)
    contained = c_high <= p_high and c_low >= p_low
    return contained and (c_high - c_low) < (p_high - p_low)


def doji(c: dict) -> bool:
    """Open and close near enough to equal that the bar decided nothing.

    A zero-range bar is not a doji -- it is a gap in the feed. Saying otherwise
    would turn every missing candle into a reversal hint at whatever level it
    happened to sit on.
    """
    high, low = float(c.get("high") or 0.0), float(c.get("low") or 0.0)
    rng = high - low
    if rng <= 0:
        return False
    body_low, body_high = _body(c)
    return (body_high - body_low) / rng <= MAX_DOJI_BODY_FRACTION


def confirmation(candles: list[dict]) -> Optional[dict]:
    """The pattern on the last CLOSED bar, or None.

    The final candle of a live series is still being written -- its close is
    the current price and will move again before the bar ends -- so it is
    skipped. Confirming on a forming bar is confirming on nothing, and it is
    the reason a backtest of this method reads better than trading it does.

    Returns `{"idx", "ts", "kind", "direction", "high", "low"}`. The extremes
    travel with the pattern because the stop is placed from them.

    The two DIRECTIONAL patterns are checked first and win. An engulfing says
    which way; a doji only says that something stalled, and an inside bar only
    that nothing happened. Letting the weak two displace the strong two would
    quietly downgrade every confirmation the method actually trades on.

    `direction` is None for the two that pick no side. The caller reads those
    as continuation in the direction already in force -- which is why they are
    usable at all in a method whose first filter is the higher-timeframe bias.
    """
    if len(candles) < 2:
        return None
    idx = len(candles) - 2
    bar = candles[idx]

    kind, direction = None, None
    if idx >= 1:
        direction = engulfing(candles[idx - 1], bar)
        kind = "engulfing" if direction else None
    if direction is None:
        direction = pin_bar(bar)
        kind = "pin_bar" if direction else None
    if direction is None and idx >= 1 and inside_bar(candles[idx - 1], bar):
        kind = "inside_bar"
    if kind is None and doji(bar):
        kind = "doji"
    if kind is None:
        return None

    return {
        "idx": idx,
        "ts": float(bar.get("ts") or 0.0),
        "kind": kind,
        "direction": direction,
        "high": float(bar.get("high") or 0.0),
        "low": float(bar.get("low") or 0.0),
    }
