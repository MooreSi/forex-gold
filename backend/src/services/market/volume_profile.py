"""Volume profile: where trade actually happened, by price.

Section 4.1 of `docs/todo/reversal-engine/200`. The engine's level map knows
where price TURNED and nothing about where it TRANSACTED. High volume nodes
are prices the market kept returning to and tend to act as magnets; low
volume nodes are prices it travelled through quickly, which is a statement
about where a target can realistically sit, not only about where to enter.

**What the volume actually is.** MT5 serves `tick_volume` for a spot gold
CFD: a count of quote changes, not traded size. It correlates with activity
and it is not size, so this is a time-and-activity-at-price profile rather
than a true market profile. A feed that omits it entirely still gives a
usable profile with every candle weighted equally, and `volume_is_proxy`
says which of the two you are looking at. Building on a proxy and saying so
beats refusing to build; building on a proxy silently does not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

DEFAULT_BINS = 50
DEFAULT_VALUE_AREA = 0.70


@dataclass(frozen=True)
class Bin:
    low: float
    high: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(frozen=True)
class Profile:
    bins: list[Bin]
    poc: float
    vah: float
    val: float
    total_volume: float
    value_area_volume: float
    volume_is_proxy: bool


def _hl(candle: dict) -> tuple[float, float]:
    hi = float(candle.get("high", candle.get("h", 0)) or 0)
    lo = float(candle.get("low", candle.get("l", 0)) or 0)
    return hi, lo


def candle_volume(candle: dict) -> float:
    """`tick_volume` (or `volume`/`real_volume`) if the feed carries one, else
    0.0. Public because `vwap` weights by the same number and two readers of
    one feed quirk must not drift apart."""
    for key in ("tick_volume", "volume", "real_volume"):
        v = candle.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def build(candles: Sequence[dict], bins: int = DEFAULT_BINS,
          value_area: float = DEFAULT_VALUE_AREA) -> Optional[Profile]:
    """Distribute each candle's volume across the prices it covered.

    Uniformly across its own high-low range, not all of it at the close. A
    candle is not a point, and assigning its whole volume to one price puts
    the busiest price wherever the bar happened to end -- on a wide bar,
    nowhere near where the trade occurred.
    """
    rows = [(hi, lo, candle_volume(c)) for c in candles or ()
            for hi, lo in [_hl(c)] if hi > 0 and lo > 0 and hi >= lo]
    if not rows:
        return None

    top = max(hi for hi, _lo, _v in rows)
    bottom = min(lo for _hi, lo, _v in rows)
    if top <= bottom:
        return None

    proxy = not any(v > 0 for _hi, _lo, v in rows)
    width = (top - bottom) / bins
    buckets = [0.0] * bins

    for hi, lo, vol in rows:
        weight = vol if not proxy else 1.0
        if weight <= 0:
            weight = 1.0
        span = hi - lo
        if span <= 0:
            idx = min(bins - 1, max(0, int((lo - bottom) / width)))
            buckets[idx] += weight
            continue
        for i in range(bins):
            b_lo = bottom + i * width
            b_hi = b_lo + width
            overlap = min(hi, b_hi) - max(lo, b_lo)
            if overlap > 0:
                buckets[i] += weight * (overlap / span)

    bin_objs = [Bin(bottom + i * width, bottom + (i + 1) * width, buckets[i])
                for i in range(bins)]
    total = sum(buckets)
    if total <= 0:
        return None

    poc_idx = max(range(bins), key=lambda i: buckets[i])
    lo_i = hi_i = poc_idx
    covered = buckets[poc_idx]
    wanted = total * max(0.0, min(1.0, value_area))
    # Expand from the point of control toward whichever neighbour is busier,
    # which is how a market profile's value area is conventionally built.
    while covered < wanted and (lo_i > 0 or hi_i < bins - 1):
        below = buckets[lo_i - 1] if lo_i > 0 else -1.0
        above = buckets[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            covered += buckets[hi_i]
        else:
            lo_i -= 1
            covered += buckets[lo_i]

    return Profile(
        bins=bin_objs, poc=bin_objs[poc_idx].mid,
        vah=bin_objs[hi_i].high, val=bin_objs[lo_i].low,
        total_volume=round(total, 6), value_area_volume=round(covered, 6),
        volume_is_proxy=proxy,
    )


def low_volume_nodes(profile: Profile, threshold_frac: float = 0.3) -> list[float]:
    """Prices where activity was below `threshold_frac` of the busiest bin."""
    if not profile or not profile.bins:
        return []
    peak = max(b.volume for b in profile.bins)
    return [b.mid for b in profile.bins if b.volume < peak * threshold_frac]


def high_volume_nodes(profile: Profile, threshold_frac: float = 0.8) -> list[float]:
    if not profile or not profile.bins:
        return []
    peak = max(b.volume for b in profile.bins)
    return [b.mid for b in profile.bins if b.volume >= peak * threshold_frac]
