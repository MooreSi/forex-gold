"""Resampling the trade sequence -- docs/todo/backtest/020.

`run_backtest` produces ONE equity path: the trades in the order they happened.
Profit factor and final balance are properties of the trade SET and do not care
about that order. Max drawdown does, entirely -- it depends on where the losing
runs fell, and one ordering cannot tell you how deep the drawdown could
plausibly have been. A strategy whose losses happened to be spread out reports
a shallow drawdown and looks safe.

That matters more here than in a general backtester, because `risk/schedule.py`
stands the system down on a daily profit target and `risk/governor.py` halts on
a daily loss limit. Both are path-dependent. A single-path backtest cannot say
how often a template would have tripped the loss halt.

Three things here are decisions rather than mechanics:

**The step is a fraction under compounding and dollars under fixed lots.**
`run_backtest` re-sizes on current equity after every trade, so a trade's dollar
P&L scales with the balance it opened on. Replaying fixed dollars would be
resampling a different system -- the one we do not run. `trade_steps` therefore
divides each P&L by the equity in front of it, and `replay` multiplies it back.
Pinned by tests/backtest/test_resample.py::test_round_trip_*, which is the test
this whole module rests on.

**A shuffle cannot move the final balance, and that is the point.** Both
compounding and addition are commutative, so permuting the trades leaves the
destination alone and moves only the path. The shuffle is therefore the honest
estimator for drawdown and says nothing about profit; it assumes nothing beyond
the trades we actually observed. The bootstrap, which draws with replacement,
answers the different and more speculative question and does move the balance.

**Trades are not independent, so the i.i.d. bootstrap is the optimistic one.**
Overlapping positions and one-way sessions mean losses arrive together. Drawing
single trades breaks those runs apart and reports a drawdown band that is too
narrow. `BLOCK` draws contiguous runs instead and is the one to believe when
the two disagree. (`market/validation.uniqueness_weights` is the other route to
the same problem and needs per-trade spans, which `BtTrade` does not carry.)

Nothing here reaches a broker, a price feed or the database. It takes a
`StrategyStats` and returns numbers. Pinned by tests/backtest/test_resample.py.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from backend.src.services.backtest.engine import StrategyStats

# How the per-trade step is expressed. Not a user choice: it follows from how
# the run was sized, and getting it wrong silently resamples another system.
COMPOUNDING = "compounding"
FIXED       = "fixed"

SHUFFLE   = "shuffle"      # permute the observed trades -- assumes nothing more
BOOTSTRAP = "bootstrap"    # draw single trades with replacement -- i.i.d.
BLOCK     = "block"        # draw contiguous runs -- keeps clustering
_METHODS  = (SHUFFLE, BOOTSTRAP, BLOCK)

DEFAULT_DRAWS = 10_000

# The same question as split.py's: how few trades is too few. The owner answers
# it once, in docs/simon-handover/036-how-few-trades-is-too-few.md. Imported
# lazily below rather than duplicated, so the two cannot drift apart.
_HANDOVER = "docs/simon-handover/036"


def _min_trades() -> int:
    from backend.src.services.backtest.split import MIN_TRADES_PER_SIDE
    return MIN_TRADES_PER_SIDE


MIN_TRADES = _min_trades()


@dataclass(frozen=True)
class Band:
    """A percentile interval. `p5`/`p95` rather than a standard deviation
    because the drawdown distribution is not symmetric and never will be."""
    p5:     float = 0.0
    median: float = 0.0
    p95:    float = 0.0


@dataclass(frozen=True)
class ResampleStats:
    method:  str = SHUFFLE
    sizing:  str = COMPOUNDING
    draws:   int = 0
    trades:  int = 0
    max_drawdown_pct: Band = field(default_factory=Band)
    final_balance:    Band = field(default_factory=Band)
    # The single path the run actually took, so the band can be read against it.
    realised_max_drawdown_pct: float = 0.0
    realised_final_balance:    float = 0.0
    # Where that path sat in its own distribution, 0-100. A high number means
    # the realised drawdown was among the deepest the same trades could give;
    # a low one means the ordering flattered the strategy.
    realised_drawdown_percentile: float = 0.0
    # Why there are no numbers, or "" when there are. Follows split.py: a row
    # of zeros beside a row showing real drawdown reads as an argument FOR the
    # strategy that was never measured.
    note: str = ""


# ── Steps and replay ─────────────────────────────────────────────────────────

def trade_steps(stats: StrategyStats, starting_balance: float,
                lots_per_trade: float = 0.0) -> tuple[list[float], str]:
    """The per-trade quantity that is invariant to where in the run it fell.

    Under fixed lots that is the dollar P&L. Under risk-percentage sizing it is
    the P&L as a fraction of the equity the trade opened on, because the lot
    size -- and so the P&L -- was computed from that equity.

    The equity in front of each trade is accumulated here rather than read from
    `stats.equity_curve`, which is rounded to 2dp for display and would make the
    round trip inexact.
    """
    trades = stats.trade_list
    if lots_per_trade > 0:
        return [float(t.pnl_usd) for t in trades], FIXED

    steps: list[float] = []
    bal = float(starting_balance)
    for t in trades:
        # A run that reached zero equity cannot express a further trade as a
        # fraction of it. Nothing sensible to return, so say so.
        if bal <= 0:
            raise ValueError("equity reached zero; cannot express steps as fractions")
        steps.append(float(t.pnl_usd) / bal)
        bal += float(t.pnl_usd)
    return steps, COMPOUNDING


def replay(steps: list[float], starting_balance: float,
           sizing: str) -> tuple[float, float]:
    """Walk a sequence of steps and return (final balance, max drawdown %).

    Mirrors `engine._compute_stats`: peak-to-trough on the running balance, and
    NO floor on the balance, so replaying the observed steps in their observed
    order reproduces the reported numbers exactly. A resampled path that reaches
    zero is ruin and stops there at 100%.
    """
    bal  = float(starting_balance)
    peak = bal
    max_dd = 0.0
    for s in steps:
        bal = bal * (1.0 + s) if sizing == COMPOUNDING else bal + s
        if bal <= 0:
            return 0.0, 100.0
        peak = max(peak, bal)
        dd = (peak - bal) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return bal, max_dd * 100.0


# ── Draws ────────────────────────────────────────────────────────────────────

def _draw(steps: list[float], method: str, rng: random.Random,
          block_size: int) -> list[float]:
    n = len(steps)
    if method == SHUFFLE:
        out = list(steps)
        rng.shuffle(out)
        return out
    if method == BOOTSTRAP:
        return [steps[rng.randrange(n)] for _ in range(n)]
    # BLOCK: moving blocks, so a run of same-sign trades can survive the draw.
    b = max(1, min(block_size, n))
    out2: list[float] = []
    last_start = n - b
    while len(out2) < n:
        i = rng.randint(0, last_start) if last_start > 0 else 0
        out2 += steps[i:i + b]
    return out2[:n]


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile. Deterministic and needs no numpy; an
    interpolating percentile would move with the draw count."""
    if not sorted_vals:
        return 0.0
    i = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[min(max(i, 0), len(sorted_vals) - 1)]


def _band(vals: list[float]) -> Band:
    s = sorted(vals)
    return Band(p5=_pct(s, 0.05), median=_pct(s, 0.50), p95=_pct(s, 0.95))


# ── Entry point ──────────────────────────────────────────────────────────────

def resample(stats: StrategyStats, starting_balance: float,
             lots_per_trade: float = 0.0, method: str = SHUFFLE,
             draws: int = DEFAULT_DRAWS, seed: int = 0,
             block_size: Optional[int] = None,
             min_trades: Optional[int] = None) -> ResampleStats:
    """A distribution for the numbers the backtest reports as points.

    Additive: the realised single path is reported alongside and is unchanged.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown resample method {method!r}; expected one of {_METHODS}")

    floor = _min_trades() if min_trades is None else min_trades
    n = len(stats.trade_list)
    if n < floor:
        return ResampleStats(
            method=method, trades=n,
            realised_max_drawdown_pct=stats.max_drawdown_pct,
            realised_final_balance=stats.final_balance,
            note=(f"{n} trades is below the {floor}-trade minimum; a band from "
                  f"this many orderings is not a measurement ({_HANDOVER})"),
        )

    steps, sizing = trade_steps(stats, starting_balance, lots_per_trade)
    realised_final, realised_dd = replay(steps, starting_balance, sizing)

    rng = random.Random(seed)
    b   = block_size if block_size is not None else max(1, int(math.sqrt(n)))
    dds: list[float] = []
    fins: list[float] = []
    for _ in range(draws):
        f, d = replay(_draw(steps, method, rng, b), starting_balance, sizing)
        dds.append(d)
        fins.append(f)

    at_or_below = sum(1 for d in dds if d <= realised_dd)
    return ResampleStats(
        method=method, sizing=sizing, draws=draws, trades=n,
        max_drawdown_pct=_band(dds),
        final_balance=_band(fins),
        realised_max_drawdown_pct=realised_dd,
        realised_final_balance=realised_final,
        realised_drawdown_percentile=round(100.0 * at_or_below / draws, 2),
    )
