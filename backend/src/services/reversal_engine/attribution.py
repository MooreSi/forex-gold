"""Post-trade attribution: which cohorts make money and which do not.

Section 5.8 of `docs/todo/reversal-engine/200`. Almost all of the analysis
in `docs/todo/data-inspect/003` was hand-written SQL on a Monday morning. A
desk has this standing, because it is the difference between managing a
strategy and periodically rediscovering it.

The axes are not a general-purpose grouping library; each one is a question
the measurements already raised:

  * **level type** -- `score_level`'s weights were calibrated against the
    reference channel's hit rate, and the owner retired that objective on
    2026-09-11. What the types are worth to THIS engine is now the question.
  * **session** -- asian 59.3% and -$938 against ny 56.2% and -$342, on
    sample sizes that differ by 3x
  * **breakeven** -- item 030's suspect: 315 trades with breakeven never
    moved at +0.767R against 126 that moved at +0.333R
  * **fill delay** -- item 040's cohort: sub-five-minute fills lost $2,142

`extra_axes` exists so the caller can add regime without this module
growing a fourth implementation of regime classification; the codebase
already has three.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

WIN_OUTCOMES = ("win", "tp")


@dataclass
class CohortStats:
    n: int = 0
    n_with_r: int = 0
    wins: int = 0
    net: float = 0.0
    _r_total: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def mean_r(self) -> Optional[float]:
        return self._r_total / self.n_with_r if self.n_with_r else None


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fill_delay_bucket(row: dict) -> str:
    """How long the signal waited before it filled.

    The boundaries are item 040's own: five minutes is where the losing
    cohort was measured, and the rest are round numbers chosen to be
    readable rather than fitted.
    """
    created = _f(row, "created_at")
    triggered = _f(row, "trigger_time")
    if created <= 0 or triggered <= 0 or triggered < created:
        return "unknown"
    mins = (triggered - created) / 60.0
    if mins < 5:
        return "under 5m"
    if mins < 30:
        return "5-30m"
    if mins < 60:
        return "30-60m"
    return "over 1h"


DEFAULT_AXES: dict[str, Callable[[dict], str]] = {
    "level_type": lambda r: str(r.get("level_type") or "unknown"),
    "session": lambda r: str(r.get("session") or "unknown"),
    "breakeven": lambda r: "moved" if r.get("sl_moved_to_be") else "not moved",
    "fill_delay": fill_delay_bucket,
}


def cohorts(rows: Iterable[dict],
            extra_axes: Optional[dict] = None) -> dict[str, dict[str, CohortStats]]:
    """Per-cohort n, win rate, mean R and net, on every axis."""
    axes = dict(DEFAULT_AXES)
    axes.update(extra_axes or {})
    out: dict[str, dict[str, CohortStats]] = {name: {} for name in axes}

    for row in rows:
        sl = _f(row, "sl_dist")
        pts = _f(row, "pnl_pts")
        # R is undefined without a stop, not zero. Averaging a fabricated
        # 0.0 in drags every cohort toward the middle and hides the thing
        # this table exists to show.
        r = (pts / sl) if sl > 0 else None
        won = str(row.get("outcome", "")).lower() in WIN_OUTCOMES

        for name, key_fn in axes.items():
            try:
                key = key_fn(row)
            except Exception:                     # noqa: BLE001
                key = "unknown"
            c = out[name].setdefault(key, CohortStats())
            c.n += 1
            c.wins += 1 if won else 0
            c.net += _f(row, "net_pnl_dollars")
            if r is not None:
                c.n_with_r += 1
                c._r_total += r
    return out


def render(table: dict[str, dict[str, CohortStats]]) -> str:
    """A fixed-width table for a log or a panel.

    Plain text on purpose: this is read in a terminal, in an email digest
    and in a panel, and one renderer that works everywhere beats three.
    """
    if not table or not any(table.values()):
        return "no closed trades to attribute"

    lines = []
    for axis, groups in table.items():
        if not groups:
            continue
        lines.append(f"{axis}")
        lines.append(f"  {'cohort':<16}{'n':>6}{'win%':>8}{'mean R':>9}{'net':>11}")
        for key, c in sorted(groups.items(), key=lambda kv: -kv[1].net):
            mean_r = "-" if c.mean_r is None else f"{c.mean_r:+.3f}"
            lines.append(f"  {key:<16}{c.n:>6}{c.win_rate * 100:>7.1f}%"
                         f"{mean_r:>9}{c.net:>11.2f}")
        lines.append("")
    return "\n".join(lines).rstrip()
