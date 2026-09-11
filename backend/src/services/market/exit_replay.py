"""What a different exit rule would have returned, on the path a trade
actually walked.

Section 5.3 of `docs/todo/reversal-engine/200`. This is how
[030](../../../../docs/todo/reversal-engine/030-wins-are-cut-at-two-thirds-of-a-r.md)
gets settled without risking a pound: replay every closed trade's real path
against a menu of candidate rules and compare. It is deliberately pure --
a path in, an R-multiple out, no database, no broker, no clock.

Three properties are load-bearing and each is pinned by a test:

  * **The stop is checked first at every point.** On a tick path that costs
    nothing, because a tick has one price and cannot reach two levels at once.
    On a BAR path it is the pessimistic resolution of an unresolvable
    ambiguity, and it matches `backtest/template_simulator.py` on purpose. A
    replay that flatters itself is worse than no replay, because its numbers
    get trusted.
  * **Breakeven LATCHES.** It arms the first time the trigger distance is
    reached and stays armed. The EA does the same, off `triggered[]` rather
    than a live price test, because re-asking the question un-armed the move
    on any retrace: 141 trades over a month never reached breakeven and closed
    a mean 66.7 pips below entry. A replay that did not latch would be pricing
    a rule nothing implements.
  * **Excursion is reported over the WHOLE path**, not truncated at the exit.
    The question being asked is what the rule left on the table.

Where a price has to be read off an ambiguous bar (a time stop, or the end of
the path), the midpoint of that bar is used. For a tick path high == low, so
that is exact; for bars it is an admitted approximation and the only one here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.src.services.market import price_path as pp
from backend.src.services.market.price_path import PathPoint


@dataclass(frozen=True)
class ExitPolicy:
    """A complete exit rule, in POINTS of the instrument (XAUUSD: 1.0 point =
    1.0 in price = 10 EA pips). Zero means "off" for every optional field, so
    the bare `ExitPolicy(stop_pts=x)` is a naked stop and nothing else."""

    stop_pts: float
    target_pts: float = 0.0
    # Fraction of the position closed at `target_pts`. 1.0 closes it all and
    # ignores `runner_target_pts`.
    target_frac: float = 1.0
    runner_target_pts: float = 0.0
    # Breakeven: arm once favourable excursion reaches the trigger, then hold
    # the stop at entry plus the buffer. 0 trigger = never.
    be_trigger_pts: float = 0.0
    be_buffer_pts: float = 0.0
    # Trail: arm at the activation distance, then hold `distance` behind the
    # best price seen, moving only in increments of `step` (0 = continuous).
    trail_activation_pts: float = 0.0
    trail_distance_pts: float = 0.0
    trail_step_pts: float = 0.0
    # Close whatever is left this many seconds after the path starts.
    time_stop_s: float = 0.0
    # Round-trip execution cost in points (spread crossed plus commission,
    # expressed in price). Charged once, in R, against the final figure.
    cost_pts: float = 0.0


@dataclass(frozen=True)
class ExitResult:
    r_multiple: float
    exit_ts: float
    reason: str          # stop | breakeven | trail | target | runner | time | path_end
    mfe_pts: float
    mae_pts: float
    partial_taken: bool


def _mid(point: PathPoint) -> float:
    return (point[1] + point[2]) / 2.0


def replay(path: list[PathPoint], entry: float, direction: str,
           policy: ExitPolicy) -> Optional[ExitResult]:
    """Walk `path` under `policy` and return what the trade would have made.

    None for an empty path: a trade with no price coverage has not been
    measured, and a fabricated 0.0R would be indistinguishable from a real
    scratch in every average computed downstream.
    """
    if policy.stop_pts <= 0:
        raise ValueError("stop_pts must be positive: it is the denominator of R")
    if not path:
        return None

    is_buy = str(direction).upper() == "BUY"
    sign = 1.0 if is_buy else -1.0
    stop_pts = float(policy.stop_pts)

    def favourable(px: float) -> float:
        return (px - entry) * sign

    # Stop as a PRICE, so a moved stop and the original are the same object.
    stop_px = entry - sign * stop_pts
    stop_reason = "stop"
    best_fav = 0.0
    realised_r = 0.0
    remaining = 1.0
    partial_taken = False
    start_ts = path[0][0]

    mfe_mae = pp.excursion(path, entry, direction) or (0.0, 0.0)

    def finish(r: float, ts: float, reason: str) -> ExitResult:
        net = r - (policy.cost_pts / stop_pts if policy.cost_pts else 0.0)
        return ExitResult(
            r_multiple=round(net, 10), exit_ts=ts, reason=reason,
            mfe_pts=mfe_mae[0], mae_pts=mfe_mae[1], partial_taken=partial_taken,
        )

    for point in path:
        ts, hi, lo = point
        adverse_px = lo if is_buy else hi
        favourable_px = hi if is_buy else lo

        # 1. The stop, always first. See the module docstring.
        if (adverse_px <= stop_px) if is_buy else (adverse_px >= stop_px):
            realised_r += remaining * (favourable(stop_px) / stop_pts)
            return finish(realised_r, ts, stop_reason)

        # 2. The first target.
        if policy.target_pts > 0 and not partial_taken:
            if favourable(favourable_px) >= policy.target_pts:
                frac = min(1.0, max(0.0, policy.target_frac))
                realised_r += frac * (policy.target_pts / stop_pts)
                remaining -= frac
                partial_taken = True
                if remaining <= 1e-9:
                    return finish(realised_r, ts, "target")

        # 3. The runner, for whatever the first target left open.
        if partial_taken and policy.runner_target_pts > 0 and remaining > 0:
            if favourable(favourable_px) >= policy.runner_target_pts:
                realised_r += remaining * (policy.runner_target_pts / stop_pts)
                return finish(realised_r, ts, "runner")

        # 4. The time stop.
        if policy.time_stop_s > 0 and (ts - start_ts) >= policy.time_stop_s:
            realised_r += remaining * (favourable(_mid(point)) / stop_pts)
            return finish(realised_r, ts, "time")

        # 5. Stop maintenance, after the exits, so a rule can never move the
        #    stop out of the way of a level the same point already reached.
        best_fav = max(best_fav, favourable(favourable_px))

        if policy.be_trigger_pts > 0 and best_fav >= policy.be_trigger_pts:
            be_px = entry + sign * policy.be_buffer_pts
            if (be_px > stop_px) if is_buy else (be_px < stop_px):
                stop_px = be_px
                stop_reason = "breakeven"

        if policy.trail_activation_pts > 0 and best_fav >= policy.trail_activation_pts:
            candidate = entry + sign * (best_fav - policy.trail_distance_pts)
            step = policy.trail_step_pts
            improves = ((candidate >= stop_px + step) if is_buy
                        else (candidate <= stop_px - step))
            if improves:
                stop_px = candidate
                stop_reason = "trail"

    last = path[-1]
    realised_r += remaining * (favourable(_mid(last)) / stop_pts)
    return finish(realised_r, last[0], "path_end")
