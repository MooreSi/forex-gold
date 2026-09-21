"""Are the frozen regime constants still true? -- docs/todo/004, phase 1.

`backend/src/utils/regime.py` blocks hours and level types for both signal
engines. Every constant in it was fitted to 510 closed signals between
2026-06-15 and 2026-07-06, and nothing since has re-read those buckets. This
prints the same buckets over whatever window you point it at, beside the
constant, so the question has an answer.

It reads. It proposes nothing, writes nothing, and reaches no broker. Changing
a constant on the strength of what it prints is phase 2 and waits on the owner
(docs/todo/004 section 4A) -- a gate that widens can open trading hours that
are shut today.

Read the two verdicts that are not `agrees` carefully:

  contradicts          the data and the constant disagree, on a cell with
                       enough trades to mean something.
  no_evidence_blocked  the cell is blocked, so it has no trades, so it cannot
                       be shown to be wrong. Not agreement. This is the count
                       that says how much of the blocklist is unfalsifiable.

Usage:
    python -m tools.regime_drift_report                     # all history
    python -m tools.regime_drift_report --days 90
    python -m tools.regime_drift_report --db /path/to.db --min-trades 10
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone

from backend.src.services.analytics import regime_drift as rd

DB = ("/Users/simon/Library/Application Support/ForexTrader/"
      "data/forex_trader_demo.db")

# Closed, executed reversal-engine signals. `net_pnl_dollars` is the figure the
# engine's own attribution uses, so the buckets here are comparable with the
# ones in regime.py's docstring.
_SQL = """
    SELECT created_at, level_type, net_pnl_dollars
    FROM re_signals
    WHERE outcome NOT IN ('open', '')
      AND close_time IS NOT NULL
      AND net_pnl_dollars IS NOT NULL
      AND created_at >= ?
"""


def load(db: str, since: float) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(_SQL, (since,))]
    finally:
        con.close()


def _ts(v: float) -> str:
    if not v:
        return "-"
    return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")


def show(report: rd.DriftReport) -> None:
    print(f"\n=== {report.engine} / {report.dimension} ===")
    print(f"constants fitted to {report.constants_from[0]} .. {report.constants_from[1]}")
    print(f"this report covers   {_ts(report.window_from)} .. {_ts(report.window_to)}")
    if report.note:
        print(f"NOTE: {report.note}")
    if report.skipped:
        print(f"{report.skipped} record(s) had no {report.dimension} and were not counted")

    print(f"\n{'cell':<12}{'trades':>7}{'total $':>11}{'win %':>8}  {'blocked':<8}verdict")
    for c in report.cells:
        print(f"{c.key:<12}{c.trades:>7}{c.total_pnl:>11.2f}{c.win_rate:>8.1f}  "
              f"{'yes' if c.blocked_today else 'no':<8}{c.verdict}")

    bad = report.contradictions
    sealed = report.unfalsifiable
    print(f"\n{len(bad)} contradiction(s): "
          f"{', '.join(c.key for c in bad) if bad else 'none'}")
    print(f"{len(sealed)} cell(s) blocked and therefore unfalsifiable: "
          f"{', '.join(c.key for c in sealed) if sealed else 'none'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--days", type=int, default=0, help="0 = all history")
    ap.add_argument("--min-trades", type=int, default=rd.MIN_TRADES)
    args = ap.parse_args(argv)

    since = time.time() - args.days * 86_400 if args.days > 0 else 0.0
    records = load(args.db, since)
    print(f"{len(records)} closed signal(s) loaded from {args.db}")

    for engine in ("bounce", "breakout"):
        show(rd.hour_drift(records, engine, min_trades=args.min_trades))
        show(rd.level_drift(records, engine, min_trades=args.min_trades))

    print("\nThis report proposes nothing. See docs/todo/004 section 4A.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
