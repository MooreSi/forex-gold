"""Fit the stop and the target to what price actually did.

Section 1.1 and phase 2 of `docs/todo/reversal-engine/200`, and the production
form of the one-off study in `tools/exit_policy_lab.py`.

The generator's geometry was reverse engineered from a Telegram channel: fixed
point targets against a stop that varies with level score, which makes TP1
0.75R on a weak level and 0.43R on a strong one. The owner retired the
requirement to resemble that channel on 2026-09-11, so the barriers are fitted
to this engine's own excursion distribution instead.

**The stop is fitted on WINNERS only.** A loser's adverse excursion is bounded
by wherever the stop happened to be, so fitting on the whole population fits
the rule already in force rather than the market. That is the measurement bias
`reversal-engine/020` warns about, and it is the difference between learning
something and confirming yourself.

**Both barriers are reported as ATR multiples as well as points**, because a
fixed point stop is a different amount of risk on a quiet day and a violent
one. The ATR multiple is what the EA template's `use_dynamic_atr` consumes, so
it is the number that actually ships.

Nothing here decides anything. It returns numbers for a human to take into a
demo session.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from backend.src.services.market import exit_replay as er

WIN_OUTCOMES = ("win", "tp", "target")


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of a sorted copy of `values`.

    Deliberately not numpy: this runs inside the app, and one more import for
    a five-line function that is called on lists of a few hundred is not a
    trade worth making.
    """
    if not values:
        raise ValueError("quantile of an empty sample")
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    pos = max(0.0, min(1.0, q)) * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


@dataclass(frozen=True)
class BarrierFit:
    stop_pts: Optional[float]
    target_pts: Optional[float]
    stop_atr_mult: Optional[float]
    target_atr_mult: Optional[float]
    rr: Optional[float]
    n_winners: int
    n_atr: int
    refusal: str = ""


def _is_win(row: dict) -> bool:
    return str(row.get("outcome", "")).lower() in WIN_OUTCOMES


def fit_barriers(observations: Iterable[dict], *, min_sample: int = 30,
                 stop_quantile: float = 0.85,
                 target_quantile: float = 0.5) -> BarrierFit:
    """Barriers implied by the excursion of trades that eventually won.

    `stop_quantile` is "what fraction of winners should survive this stop":
    0.85 keeps roughly 85% of them and cuts the rest short, which is the
    trade being made explicit rather than guessed. `target_quantile` is how
    far into the winners' reach distribution to place the target; the median
    is the honest default, because half of them get there.
    """
    rows = [r for r in observations if _is_win(r)
            and r.get("mfe_pts") is not None and r.get("mae_pts") is not None]
    if len(rows) < min_sample:
        return BarrierFit(None, None, None, None, None, len(rows), 0,
                          refusal=(f"sample of {len(rows)} winners is below the "
                                   f"{min_sample} needed to fit a barrier that "
                                   f"will be acted on"))

    stop = quantile([float(r["mae_pts"]) for r in rows], stop_quantile)
    target = quantile([float(r["mfe_pts"]) for r in rows], target_quantile)

    atr_rows = [r for r in rows if float(r.get("atr") or 0.0) > 0.0]
    stop_mult = target_mult = None
    if atr_rows:
        stop_mult = quantile([float(r["mae_pts"]) / float(r["atr"])
                              for r in atr_rows], stop_quantile)
        target_mult = quantile([float(r["mfe_pts"]) / float(r["atr"])
                                for r in atr_rows], target_quantile)

    return BarrierFit(
        stop_pts=round(stop, 3), target_pts=round(target, 3),
        stop_atr_mult=None if stop_mult is None else round(stop_mult, 3),
        target_atr_mult=None if target_mult is None else round(target_mult, 3),
        rr=round(target / stop, 3) if stop > 0 else None,
        n_winners=len(rows), n_atr=len(atr_rows),
    )


# ── Policy sweep ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepRow:
    stop_pts: float
    target_pts: float
    expectancy: float
    n: int
    win_rate: float
    ci_low: float
    ci_high: float
    first_half: float
    second_half: float


def _replayed(trades: Iterable[dict], policy: er.ExitPolicy) -> list[float]:
    """R-multiples, skipping trades with no price coverage.

    Skipping rather than scoring 0.0: a trade whose path could not be
    reconstructed has not been measured, and counting it as a scratch drags
    every expectancy toward zero by an amount nobody can see.
    """
    out = []
    for t in trades:
        res = er.replay(t.get("path") or [], float(t["entry"]),
                        str(t["direction"]), policy)
        if res is not None:
            out.append(res.r_multiple)
    return out


def expectancy(trades: Iterable[dict], policy: er.ExitPolicy) -> float:
    rs = _replayed(trades, policy)
    return sum(rs) / len(rs) if rs else 0.0


def bootstrap_ci(rs: Sequence[float], n: int = 2000,
                 seed: int = 1337) -> tuple[float, float]:
    """95% interval for the mean, by resampling.

    Seeded on purpose. An interval that moves every time it is computed is
    one nobody can quote in a decision, and the seed costs nothing because
    the question being asked is "does this straddle zero", not "what is the
    exact bound".
    """
    if not rs:
        return 0.0, 0.0
    rng = random.Random(seed)
    k = len(rs)
    means = sorted(sum(rng.choice(rs) for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)], means[min(n - 1, int(0.975 * n))]


def sweep(trades: Sequence[dict], stops: Sequence[float],
          targets: Sequence[float], *, cost_pts: float = 0.0,
          bootstrap: int = 0, be_trigger_pts: float = 0.0,
          target_frac: float = 1.0,
          runner_target_pts: float = 0.0) -> list[SweepRow]:
    """Expectancy for every stop/target pair, best first.

    Each row also carries a chronological split. A configuration that wins on
    the whole sample and loses on its own second half has been fitted to the
    first half, and that is the single cheapest overfitting check there is.
    """
    ordered = sorted(trades, key=lambda t: float(t.get("close_time") or 0.0))
    half = len(ordered) // 2
    rows: list[SweepRow] = []

    for s in stops:
        for tgt in targets:
            policy = er.ExitPolicy(
                stop_pts=s, target_pts=tgt, target_frac=target_frac,
                runner_target_pts=runner_target_pts,
                be_trigger_pts=be_trigger_pts, cost_pts=cost_pts,
            )
            rs = _replayed(ordered, policy)
            if not rs:
                continue
            lo, hi = bootstrap_ci(rs, bootstrap) if bootstrap else (0.0, 0.0)
            rows.append(SweepRow(
                stop_pts=s, target_pts=tgt,
                expectancy=sum(rs) / len(rs), n=len(rs),
                win_rate=sum(1 for r in rs if r > 0) / len(rs),
                ci_low=lo, ci_high=hi,
                first_half=expectancy(ordered[:half], policy) if half else 0.0,
                second_half=expectancy(ordered[half:], policy) if half else 0.0,
            ))

    rows.sort(key=lambda r: r.expectancy, reverse=True)
    return rows
