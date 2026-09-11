"""Confirmation at the level, rather than mere arrival at it.

Section 4.2 of `docs/todo/reversal-engine/200`. The reversal engine fires
when price comes within `PROXIMITY_THRESHOLD_PTS` of a ranked level. Location
with no trigger is the mechanism behind the instant-fill cohort
`reversal-engine/040` measured at -$2,142: price arriving fast at a level and
filling immediately is precisely the case where the level is about to fail.

Three checks, all computable from the M1 candles the bridge already serves:

  * **rejection** -- price traded through the level and closed back inside it
  * **deceleration** -- price is arriving slowly relative to its own ATR
  * **sweep and reclaim** -- the liquidity below/above the level was taken and
    price got back through it within a bounded number of bars

**Every check is off by default** and `confirm` with a default config passes
anything, so installing this changes nothing about what the engine trades
until a dial moves. **An indeterminate check counts as a failure**, never a
pass: treating "could not evaluate" as "confirmed" is how a gate quietly
stops gating, which is the failure this repo's rules exist to catch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence


@dataclass(frozen=True)
class TriggerConfig:
    require_rejection: bool = False
    require_deceleration: bool = False
    require_sweep_reclaim: bool = False
    deceleration_bars: int = 3
    # Mean range of the last N bars, as a fraction of ATR, at or below which
    # price counts as arriving slowly.
    max_range_ratio: float = 0.5
    sweep_within_bars: int = 3


@dataclass(frozen=True)
class TriggerResult:
    passed: bool
    checks: dict = field(default_factory=dict)
    reason: str = ""


def _hlc(candle: dict) -> tuple[float, float, float]:
    h = float(candle.get("high", candle.get("h", 0)) or 0)
    l = float(candle.get("low", candle.get("l", 0)) or 0)
    c = float(candle.get("close", candle.get("c", 0)) or 0)
    return h, l, c


def rejection(candles: Sequence[dict], level: float, direction: str) -> bool:
    """Did the LAST closed candle trade through the level and close back?

    Both halves are required. Closing above a level price never reached is
    just being above it, and without that condition the check fires on every
    candle of an uptrend.
    """
    if not candles:
        return False
    h, l, c = _hlc(candles[-1])
    if h <= 0 or l <= 0 or c <= 0:
        return False
    if str(direction).upper() == "BUY":
        return l < level and c > level
    return h > level and c < level


def decelerating(candles: Sequence[dict], bars: int, atr: float,
                 max_ratio: float = 0.5) -> Optional[bool]:
    """Is price arriving slowly, measured against its own volatility?

    The mean true range of the last `bars` candles as a fraction of ATR. Not
    a derivative of speed: what matters for a level holding is the SIZE of
    the bars walking into it, and expressing that against ATR is what makes
    the threshold mean the same thing on a quiet day and a violent one.

    None when ATR is unavailable -- the question cannot be answered, and the
    caller must not read that as a pass.
    """
    if atr <= 0 or bars <= 0 or len(candles) < bars:
        return None
    window = candles[-bars:]
    ranges = []
    for cd in window:
        h, l, _c = _hlc(cd)
        if h <= 0 or l <= 0:
            return None
        ranges.append(h - l)
    if not ranges:
        return None
    return (sum(ranges) / len(ranges)) <= atr * max_ratio


def sweep_reclaimed(candles: Sequence[dict], pool: float, direction: str,
                    within_bars: int = 3) -> bool:
    """Was the liquidity at `pool` taken, and then reclaimed in time?

    Measured from the FIRST sweep in the window, not the most recent. Using
    the most recent makes the check self-fulfilling: a candle that dips
    through the pool and closes back above it is simultaneously the newest
    sweep and its own reclaim, so the rule would pass on any wick. The
    caller is expected to pass a bounded recent window of candles.
    """
    if not candles:
        return False
    is_buy = str(direction).upper() == "BUY"
    swept_at = None
    for i, cd in enumerate(candles):
        h, l, _c = _hlc(cd)
        if (l < pool) if is_buy else (h > pool):
            swept_at = i
            break
    if swept_at is None:
        return False

    for cd in candles[swept_at:swept_at + within_bars + 1]:
        _h, _l, c = _hlc(cd)
        if c <= 0:
            continue
        if (c > pool) if is_buy else (c < pool):
            return True
    return False


def confirm(candles: Sequence[dict], level: float, direction: str,
            atr: float, cfg: TriggerConfig,
            pool: Optional[float] = None) -> TriggerResult:
    """Run every REQUIRED check. All must pass; an unevaluable one fails."""
    checks: dict = {}

    if cfg.require_rejection:
        checks["rejection"] = rejection(candles, level, direction)

    if cfg.require_deceleration:
        checks["deceleration"] = decelerating(
            candles, cfg.deceleration_bars, atr, cfg.max_range_ratio)

    if cfg.require_sweep_reclaim:
        checks["sweep_reclaim"] = sweep_reclaimed(
            candles, pool if pool is not None else level, direction,
            cfg.sweep_within_bars)

    failed = [name for name, ok in checks.items() if ok is not True]
    return TriggerResult(
        passed=not failed, checks=checks,
        reason="" if not failed else "not confirmed: " + ", ".join(sorted(failed)),
    )
