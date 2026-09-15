"""In-sample / out-of-sample split for a backtest run -- docs/todo/003.

The comparison table reports one number per strategy over the whole loaded
window, and the constants that number validates were chosen by looking at that
same window. `engine.py:33-39` records `+0.167R/trade at 88.7% win rate` from
the 259 signals that picked `_GDVR_SL_MULT = 4.0`. Part of that figure is the
strategy and part of it is the choosing. This module separates them.

Three things here are decisions rather than mechanics, and each has a reason:

**Each side is its own account, from the same starting balance.** `run_backtest`
recomputes lot size on current equity after every trade, so a side continued
from the other side's closing balance has its dollar P&L scaled by how the
first half went -- a good in-sample half inflates out-of-sample lots, and then
out-of-sample dollars. That is the exact contamination this module exists to
remove. It also matches the guarantee `run_backtest` already makes: "Each
strategy gets an independent account; results are not cross-contaminated."

**The partition is by `created_ts`, sorted, never by list position.**
`repo.fetch_backtest_signals` orders by `created_at`, so a positional slice
looks right against every DB-sourced run and silently stops splitting by time
the moment a manual signal is added.

**A side too thin to mean anything reports a note, not a number.** Four trades
at 75% is not a 75% win rate, it is three wins. Where that line sits is a
judgment about believability rather than a computation, so it is the owner's:
`MIN_TRADES_PER_SIDE` is provisional and the open decision is recorded in
docs/simon-handover/036-how-few-trades-is-too-few.md. This follows the
precedent already set by `unsupported_reason` -- a row of zeros beside a row
showing real drawdown reads as an argument FOR the strategy that was never
measured.

Nothing here reaches a broker, a price feed or the database. It partitions the
signal list it is handed and calls back into the walk. Pinned by
tests/backtest/test_split.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from backend.src.services.backtest.engine import BtSignal, StrategyStats

# Provisional. See docs/simon-handover/036-how-few-trades-is-too-few.md --
# the owner sets this, and until they do it is visible on screen rather than
# buried here.
MIN_TRADES_PER_SIDE = 20

_HANDOVER = "docs/simon-handover/036"


@dataclass
class SplitStats:
    """Two halves of one strategy's run, and where the cut fell."""
    boundary_ts:        float              # unix epoch UTC: first OOS signal
    requested_frac:     float              # what was asked for
    achieved_frac:      float              # what the tie rule actually gave
    in_sample:          Optional[StrategyStats]
    out_of_sample:      Optional[StrategyStats]
    in_sample_note:     str = ""           # why there is no number, when there isn't
    out_of_sample_note: str = ""


def partition(
    signals: list[BtSignal], fraction: float
) -> tuple[list[BtSignal], list[BtSignal], float]:
    """Split `signals` by creation time at `fraction`, and say where it landed.

    Tied timestamps never straddle the boundary: the cut is walked forward
    until the timestamp changes, so two signals created at the same moment --
    which the tuner necessarily saw together -- cannot end up on opposite
    sides of a line that claims to separate what it saw from what it did not.
    The returned fraction is therefore what happened, not what was requested.
    """
    ordered = sorted(signals, key=lambda s: s.created_ts)
    n = len(ordered)
    if n == 0:
        return [], [], 0.0

    k = max(0, min(n, int(round(n * fraction))))
    while 0 < k < n and ordered[k].created_ts == ordered[k - 1].created_ts:
        k += 1

    return ordered[:k], ordered[k:], k / n


def _thin_note(trades: int, min_trades: int) -> str:
    return (f"{trades} trades -- too few to measure "
            f"(min {min_trades}, provisional; see {_HANDOVER})")


def split_stats(
    stats:      StrategyStats,
    signals:    list[BtSignal],
    fraction:   float,
    min_trades: int,
    run_side:   Callable[[list[BtSignal]], StrategyStats],
) -> Optional[SplitStats]:
    """Run both sides of the split, or return None if there is no split to make.

    None means the combined row stands alone, and it is returned when:

    - the template was refused. `unsupported_reason` and a thin side are two
      different silences, and splitting a refusal produces two halves of
      nothing. Two rows of zeros read worse than one.
    - no split was requested, or the fraction leaves one side empty.
    """
    if stats.unsupported_reason:
        return None
    if not 0.0 < fraction < 1.0:
        return None

    in_sigs, out_sigs, achieved = partition(signals, fraction)
    if not in_sigs or not out_sigs:
        return None

    in_stats  = run_side(in_sigs)
    out_stats = run_side(out_sigs)

    in_note  = "" if in_stats.trades  >= min_trades else _thin_note(in_stats.trades,  min_trades)
    out_note = "" if out_stats.trades >= min_trades else _thin_note(out_stats.trades, min_trades)

    return SplitStats(
        boundary_ts        = out_sigs[0].created_ts,
        requested_frac     = fraction,
        achieved_frac      = achieved,
        in_sample          = in_stats  if not in_note  else None,
        out_of_sample      = out_stats if not out_note else None,
        in_sample_note     = in_note,
        out_of_sample_note = out_note,
    )
