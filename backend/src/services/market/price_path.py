"""The price path a trade actually saw.

Every measurement in `docs/todo/reversal-engine/200` walks a path built here:
excursion backfill, counterfactual exit replay, and barrier fitting. One
representation serves all three.

A path point is `(ts, high, low)`:

  * from a TICK, `high == low` -- there is no intrabar ambiguity, which is the
    whole reason to prefer ticks. When a tick path shows the stop being
    reached before the target, that is what happened, not an assumption.
  * from a BAR, `high != low` and the order of the two extremes inside the bar
    is unknown. `is_ambiguous` marks those so a replay can resolve them
    pessimistically rather than silently flattering itself, the same choice
    `backtest/template_simulator.py` already makes.

**Side of the book.** A long is closed at the BID and a short at the ASK, so
the path a long saw is the bid series and the path a short saw is the ask
series. Walking the mid instead understates the adverse excursion of every
trade by half the spread, in the same direction every time, which is exactly
the leakage `reversal-engine/020` is trying to explain. Bars from MT5 are bid
based, so a bar path is correct for a long and optimistic by the spread for a
short; `build_bar_path` says so and takes no direction for that reason.
"""
from __future__ import annotations

from typing import Optional

# (ts, high, low)
PathPoint = tuple[float, float, float]


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def exit_side_price(tick: dict, direction: str) -> float:
    """The price this trade would be CLOSED at, given a tick. 0.0 if the tick
    does not carry the side we need -- callers drop those rather than
    substituting the other side, because a bid standing in for an ask is a
    silent half-spread error, not a missing value."""
    if str(direction).upper() == "BUY":
        return _f(tick.get("bid"))
    return _f(tick.get("ask"))


def build_tick_path(ticks, direction: str) -> list[PathPoint]:
    """Ordered `(ts, price, price)` points from raw bridge ticks.

    Ticks whose relevant side is missing or zero are dropped: MT5 reports 0.0
    for "no quote", and a 0.0 price walked as real would register as the most
    adverse excursion ever seen on every trade it touched.
    """
    out: list[PathPoint] = []
    for t in ticks or ():
        px = exit_side_price(t, direction)
        if px <= 0.0:
            continue
        ts = _f(t.get("time") or t.get("ts"))
        out.append((ts, px, px))
    out.sort(key=lambda p: p[0])
    return out


def build_bar_path(bars) -> list[PathPoint]:
    """Ordered `(ts, high, low)` points from OHLC candles.

    Takes no direction: MT5 candles are bid based for both sides, and
    pretending otherwise would invent an ask series that was never quoted.
    """
    out: list[PathPoint] = []
    for b in bars or ():
        hi = _f(b.get("high", b.get("h")))
        lo = _f(b.get("low", b.get("l")))
        if hi <= 0.0 or lo <= 0.0:
            continue
        ts = _f(b.get("ts") or b.get("time"))
        out.append((ts, hi, lo))
    out.sort(key=lambda p: p[0])
    return out


def is_ambiguous(point: PathPoint) -> bool:
    """True when this point spans a range and the order of its extremes is
    unknown. False for a tick."""
    return point[1] != point[2]


def excursion(path: list[PathPoint], entry: float,
              direction: str) -> Optional[tuple[float, float]]:
    """`(mfe_pts, mae_pts)` as POSITIVE distances from entry, or None.

    None rather than `(0.0, 0.0)` for an empty path. A signal with no tick
    coverage has not been measured, and recording it as one that never moved
    would poison every fit downstream with a fabricated observation -- the
    same class of error as the fabricated $0.00 closes in
    `reversal-engine/010`.

    Both figures are floored at zero: a trade that only ever went one way has
    no excursion on the other side, and `measure_repo.record_excursion` stores
    positive distances and MAXes them, so a negative would be indistinguishable
    from no observation at all.
    """
    if not path:
        return None
    is_buy = str(direction).upper() == "BUY"
    best = 0.0
    worst = 0.0
    for _ts, hi, lo in path:
        if is_buy:
            best = max(best, hi - entry)
            worst = max(worst, entry - lo)
        else:
            best = max(best, entry - lo)
            worst = max(worst, hi - entry)
    return round(max(0.0, best), 4), round(max(0.0, worst), 4)
