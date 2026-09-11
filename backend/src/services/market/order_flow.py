"""Order flow, scoped to what this feed can actually answer.

Section 4.3 of `docs/todo/reversal-engine/200`. Professional gold desks read
cumulative delta, footprint imbalance and absorption at the level. Two things
stood in the way here, and only one of them is now fixed:

  1. `mt5_bridge._get_ticks_range` asked for `COPY_TICKS_ALL` and then dropped
     `flags`, `last` and `volume` in its dict comprehension. Passed through
     since 2026-09-11.
  2. **A retail spot-CFD feed often publishes bid/ask quotes only.** MT5's
     tick structure defines `TICK_FLAG_BUY` and `TICK_FLAG_SELL`, but a
     broker that never publishes a Last has no trade side to report, and then
     true delta does not exist at all.

So the first function here is a PROBE, not a calculation, and every result
carries the METHOD that produced it. A tick-rule proxy is a legitimate
measurement and it is not measured delta; a feature that cannot tell you
which one it is will eventually be quoted as if it were the latter.

Everything here is read-only and decides nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# MQL5 ENUM_TICK_FLAG. Values are the broker protocol's, not ours.
TICK_FLAG_BID = 2
TICK_FLAG_ASK = 4
TICK_FLAG_LAST = 8
TICK_FLAG_VOLUME = 16
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64


@dataclass(frozen=True)
class FeedCapability:
    n_ticks: int
    has_trade_side: bool
    has_last: bool
    has_volume: bool
    note: str


@dataclass(frozen=True)
class DeltaResult:
    delta: float
    method: str          # "trade_flags" (measured) | "tick_rule" (proxy)
    n: int
    bucket_ts: float = 0.0


@dataclass(frozen=True)
class SpreadStats:
    mean_pts: float
    max_pts: float
    last_pts: float
    widening_ratio: float
    n: int


def _f(tick: dict, key: str) -> float:
    try:
        return float(tick.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _flags(tick: dict) -> int:
    try:
        return int(tick.get("flags") or 0)
    except (TypeError, ValueError):
        return 0


def _mid(tick: dict) -> float:
    bid, ask = _f(tick, "bid"), _f(tick, "ask")
    if bid <= 0 or ask <= 0:
        return 0.0
    return (bid + ask) / 2.0


def probe_feed(ticks: Sequence[dict]) -> FeedCapability:
    """What this broker's tick stream actually carries.

    Run this before designing anything on top of delta. "The feed has no
    trade side" and "the market was quiet" produce the same empty delta, and
    only one of them means the measurement is impossible.
    """
    if not ticks:
        return FeedCapability(0, False, False, False,
                              "no ticks in the probe window: nothing can be "
                              "concluded about this feed")
    side = any(_flags(t) & (TICK_FLAG_BUY | TICK_FLAG_SELL) for t in ticks)
    last = any(_f(t, "last") > 0 for t in ticks)
    vol = any(_f(t, "volume") > 0 for t in ticks)
    note = ("trade side present: cumulative delta is measurable"
            if side else
            "quote-only feed: no trade side, so delta can only ever be a "
            "tick-rule proxy on this instrument")
    return FeedCapability(len(ticks), side, last, vol, note)


def _tick_rule_signs(ticks: Sequence[dict]) -> list[float]:
    """+1 for an uptick, -1 for a downtick, 0 for an unchanged or unusable
    mid. The first tick has no predecessor and so carries no information."""
    signs = [0.0]
    prev = _mid(ticks[0]) if ticks else 0.0
    for t in ticks[1:]:
        mid = _mid(t)
        if mid <= 0 or prev <= 0:
            signs.append(0.0)
        elif mid > prev:
            signs.append(1.0)
        elif mid < prev:
            signs.append(-1.0)
        else:
            signs.append(0.0)
        if mid > 0:
            prev = mid
    return signs


def cumulative_delta(ticks: Sequence[dict]) -> Optional[DeltaResult]:
    """Signed flow over the window, measured if the feed allows it.

    None rather than 0.0 when there is nothing to classify: zero delta means
    balanced flow, and a model cannot distinguish that from "no observation"
    if both arrive as the same number.
    """
    if not ticks:
        return None

    sided = [t for t in ticks if _flags(t) & (TICK_FLAG_BUY | TICK_FLAG_SELL)]
    if sided:
        total = 0.0
        for t in sided:
            size = _f(t, "volume") or 1.0
            total += size if _flags(t) & TICK_FLAG_BUY else -size
        return DeltaResult(round(total, 6), "trade_flags", len(sided),
                           _f(ticks[0], "time"))

    if len(ticks) < 2:
        return None
    signs = _tick_rule_signs(ticks)
    return DeltaResult(round(sum(signs), 6), "tick_rule", len(ticks),
                       _f(ticks[0], "time"))


def rolling_delta(ticks: Sequence[dict], bucket_s: float) -> list[DeltaResult]:
    """Delta per fixed time bucket, so divergence against price can be seen.

    Price making a new high while delta does not is the classic absorption
    signal. That comparison needs a SERIES, which one cumulative figure
    cannot provide.
    """
    if not ticks or bucket_s <= 0:
        return []
    ordered = sorted(ticks, key=lambda t: _f(t, "time"))
    t0 = _f(ordered[0], "time")
    groups: dict[int, list[int]] = {}
    for i, t in enumerate(ordered):
        idx = int((_f(t, "time") - t0) // bucket_s)
        groups.setdefault(idx, []).append(i)

    # Signs are computed over the WHOLE series, then attributed to buckets.
    # Computing them per bucket would throw away the first tick of every
    # bucket, which on a short bucket is most of the information.
    signs = _tick_rule_signs(ordered)
    has_side = any(_flags(t) & (TICK_FLAG_BUY | TICK_FLAG_SELL) for t in ordered)
    out = []
    for idx in sorted(groups):
        members = groups[idx]
        if has_side:
            total = 0.0
            for i in members:
                t = ordered[i]
                if not _flags(t) & (TICK_FLAG_BUY | TICK_FLAG_SELL):
                    continue
                size = _f(t, "volume") or 1.0
                total += size if _flags(t) & TICK_FLAG_BUY else -size
            method = "trade_flags"
        else:
            total = sum(signs[i] for i in members)
            method = "tick_rule"
        out.append(DeltaResult(round(total, 6), method, len(members),
                               t0 + idx * bucket_s))
    return out


def tick_arrival_rate(ticks: Sequence[dict],
                      window_s: float) -> Optional[float]:
    """Ticks per second over the last `window_s`.

    A surge in quote updates into a level is the cheapest available proxy
    for participation on a feed with no trade size.
    """
    if not ticks or window_s <= 0:
        return None
    end = max(_f(t, "time") for t in ticks)
    n = sum(1 for t in ticks if _f(t, "time") >= end - window_s)
    return round(n / window_s, 6)


def spread_stats(ticks: Sequence[dict]) -> SpreadStats:
    """Mean, worst and trend of the spread over the window.

    A spread widening as price approaches a level is liquidity being pulled,
    and it is simultaneously your cost: 0.6 points against the reversal
    engine's mean 5.75 point stop is over 10% of R.
    """
    spreads = []
    for t in ticks or ():
        bid, ask = _f(t, "bid"), _f(t, "ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        spreads.append(ask - bid)
    if not spreads:
        return SpreadStats(0.0, 0.0, 0.0, 0.0, 0)

    half = len(spreads) // 2
    early = spreads[:half] or spreads
    late = spreads[half:] or spreads
    early_mean = sum(early) / len(early)
    ratio = (sum(late) / len(late)) / early_mean if early_mean > 0 else 0.0

    return SpreadStats(
        mean_pts=round(sum(spreads) / len(spreads), 6),
        max_pts=round(max(spreads), 6), last_pts=round(spreads[-1], 6),
        widening_ratio=round(ratio, 6), n=len(spreads),
    )
