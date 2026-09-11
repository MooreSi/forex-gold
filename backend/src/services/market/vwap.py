"""VWAP and its standard-deviation bands.

Section 4.1 of `docs/todo/reversal-engine/200`. VWAP is the most widely
referenced intraday price on a real desk: institutional execution is
benchmarked against it, which is exactly why price reacts around it. This app
has never computed one.

Anchoring is a parameter rather than a policy. Session VWAP is `anchor_ts` at
the session open; anchored VWAP from a swing, a news event or the week's open
is the same function with a different anchor, and both are useful for
different questions.

Volume is `tick_volume` on this feed -- a count of quote changes, not size --
so `volume_is_proxy` reports when the weighting fell back to equal weights.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.src.services.market.volume_profile import candle_volume


@dataclass(frozen=True)
class VwapResult:
    vwap: float
    sd: float
    upper_1sd: float
    lower_1sd: float
    upper_2sd: float
    lower_2sd: float
    n: int
    volume_is_proxy: bool


def _typical(candle: dict) -> float:
    hi = float(candle.get("high", candle.get("h", 0)) or 0)
    lo = float(candle.get("low", candle.get("l", 0)) or 0)
    cl = float(candle.get("close", candle.get("c", 0)) or 0)
    if hi <= 0 or lo <= 0 or cl <= 0:
        return 0.0
    return (hi + lo + cl) / 3.0


def vwap(candles: Sequence[dict],
         anchor_ts: Optional[float] = None) -> Optional[VwapResult]:
    """Volume-weighted average of the typical price from `anchor_ts` onward."""
    rows = []
    for c in candles or ():
        ts = float(c.get("ts") or c.get("time") or 0.0)
        if anchor_ts is not None and ts < anchor_ts:
            continue
        tp = _typical(c)
        if tp <= 0:
            continue
        rows.append((tp, candle_volume(c)))

    if not rows:
        return None

    proxy = not any(v > 0 for _tp, v in rows)
    weights = [1.0 if proxy or v <= 0 else v for _tp, v in rows]
    total_w = sum(weights)
    if total_w <= 0:
        return None

    mean = sum(tp * w for (tp, _v), w in zip(rows, weights)) / total_w
    var = sum(w * (tp - mean) ** 2 for (tp, _v), w in zip(rows, weights)) / total_w
    sd = math.sqrt(max(0.0, var))

    return VwapResult(
        vwap=round(mean, 5), sd=round(sd, 5),
        upper_1sd=round(mean + sd, 5), lower_1sd=round(mean - sd, 5),
        upper_2sd=round(mean + 2 * sd, 5), lower_2sd=round(mean - 2 * sd, 5),
        n=len(rows), volume_is_proxy=proxy,
    )
