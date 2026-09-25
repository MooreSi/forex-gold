"""What actually happened to a signal, decided in one place.

`vantage_signals.status` records how a signal ENDED -- active, closed,
expired, cancelled -- and "closed" is the value that carries no information at
all. A signal that took 300 dollars and one that gave back 300 both read
"closed", which made the Dashboard's feed a list of finished things with no
way to tell which of them had worked (owner, 2026-09-22).

The answer is not on the signal. It is the net profit of the trades that
signal produced, in `vantage_simulated_trades` keyed by `signal_id`. This
module is the only place that turns those rows into a word, for the same
reason every other money figure has exactly one source: two screens that each
sum `net_pnl` for themselves are two screens that can disagree about the same
signal, and the one being believed would be whichever the operator looked at
last.

Three things the rule is deliberate about, because each of them is a way to
overstate a record:

**Break-even is its own answer.** A trade closed at the entry after the stop
was moved really happened and really made nothing. Rounding it into "won"
would inflate the hit rate by every scratch trade the account has taken.

**An open trade has no outcome.** Running profit is not a result. A position
40 dollars up that is still open has won nothing, and a feed that says
otherwise is counting money the account does not have.

**A trade cannot have no P&L.** `vantage_simulated_trades.net_pnl` is
`REAL NOT NULL DEFAULT 0`, so there is no "unknown" row to defend against --
the first version of this module guarded one and the guard was dead code.
`classify` still answers `None` for a missing number, because it is a public
rule and a caller with a figure from somewhere else must not be told "flat"
when the truth is "unknown".
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

#: What this module will ever say about a resolved signal.
OUTCOMES = ("won", "lost", "flat")


def classify(net_pnl: Optional[float]) -> Optional[str]:
    """The word for a net profit. `None` when there is no number to read.

    Zero is `"flat"` and not `"won"`. See the module docstring.
    """
    if net_pnl is None:
        return None
    try:
        value = float(net_pnl)
    except (TypeError, ValueError):
        return None
    if value > 0:
        return "won"
    if value < 0:
        return "lost"
    return "flat"


def outcomes_by_signal() -> dict[str, dict[str, Any]]:
    """`{signal_id: {"outcome": ..., "net_pnl": ...}}` for every RESOLVED signal.

    One grouped query for the whole table, not a lookup per signal: the
    owner's database holds 608 signals and this read is polled every ten
    seconds by an open Dashboard, so a per-row query is 608 round trips into
    SQLite on every tick. `test_the_outcome_query_runs_once_for_the_whole_read`
    is what keeps it that way.

    A signal with no closed trade is simply absent from the mapping. The
    caller decides what to show for it; this module does not invent one.
    """
    # The rows come from the repo: SQL lives in a repo file and nowhere else,
    # which the structure gate enforces at a count it will not let rise. This
    # module is the RULE -- what those numbers mean -- and the rule is what
    # must not exist twice.
    #
    # Imported here rather than at module scope: `repo` imports this module
    # for `attach`, and a top-level import in both directions is a cycle.
    from backend.src.services.signals.repo import trade_totals_by_signal

    try:
        rows = trade_totals_by_signal()
    except Exception as exc:
        # A feed that loses its colour coding is a worse screen; a feed that
        # 500s is no screen at all. The signals read must survive this.
        log.warning("[signals] could not read trade outcomes: %s", exc)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        signal_id = row.get("signal_id")
        if not signal_id:
            continue
        # Still running, in whole or in part.
        if int(row.get("closed_legs") or 0) != int(row.get("legs") or 0):
            continue
        if int(row.get("closed_legs") or 0) == 0:
            continue
        outcome = classify(row.get("net"))
        if outcome is None:
            continue
        out[str(signal_id)] = {"outcome": outcome, "net_pnl": float(row["net"])}
    return out


def attach(rows: list[dict]) -> list[dict]:
    """Add `outcome` and `net_pnl` to signal rows, in place.

    Both keys are always set, `None` where there is nothing to say. A renderer
    that has to tell "no outcome" from "the field is missing" will get it
    wrong once, and this is the read four screens share.
    """
    if not rows:
        return rows
    found = outcomes_by_signal()
    for row in rows:
        hit = found.get(str(row.get("signal_id") or ""))
        row["outcome"] = hit["outcome"] if hit else None
        row["net_pnl"] = hit["net_pnl"] if hit else None
    return rows
