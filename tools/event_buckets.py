#!/usr/bin/env python3
"""Are signals taken just after a scheduled release worse than the rest?

The arithmetic lives in `backend/src/services/backtest/event_buckets.py`
(tested there); this file fetches, joins and prints.

READ-ONLY. It opens `reversal_engine.db` / `breakout_signal.db` through
`mode=ro` URIs and touches no broker, network or model.

Entry time is `trigger_time` (when the signal actually entered), falling back
to `created_at`. Label:
  reversal  the v9 realised R (`_realised_r`); executed rows carry the broker's
            profit at the real lot and are left out, as the model leaves them out
  breakout  the model's label: rr_tp1 on a win, -1 on a loss, 0 on breakeven
            (planned R, not realised, so read it as direction only)

Events: NFP and the FOMC statement are generated from rules. Add the rest with
`--events file.csv` (`utc,tier,title`, ISO timestamps WITH an offset). The live
calendar feed keeps only the current week, so there is no history to read.

Usage:
    .venv/bin/python -m tools.event_buckets [--engine reversal|breakout]
                                            [--since 2026-07-20]
                                            [--events extra.csv]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.backtest import event_buckets as eb
from backend.src.services.reversal_engine.ml_engine._training_data import _realised_r


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def reversal_rows(since: float) -> list[dict]:
    with _ro(DATA_DIR / "reversal_engine.db") as c:
        raw = [dict(r) for r in c.execute(
            "SELECT created_at, trigger_time, status, live_exec_status, sl_dist,"
            " net_pnl_dollars FROM re_signals WHERE created_at >= ? AND status='closed'",
            (since,))]
    return [{"ts": r["trigger_time"] or r["created_at"],
             "r": _realised_r(r)} for r in raw]


def breakout_rows(since: float) -> list[dict]:
    with _ro(DATA_DIR / "breakout_signal.db") as c:
        raw = [dict(r) for r in c.execute(
            "SELECT created_at, trigger_time, outcome, rr_tp1 FROM bo_signals"
            " WHERE created_at >= ? AND status='closed'"
            " AND outcome IN ('win','loss','be')", (since,))]
    out = []
    for r in raw:
        rr = float(r["rr_tp1"] or 1.0)
        label = rr if r["outcome"] == "win" else (-1.0 if r["outcome"] == "loss" else 0.0)
        out.append({"ts": r["trigger_time"] or r["created_at"], "r": label})
    return out


def render(buckets: list, n_rows: int, n_events: int) -> str:
    lines = [f"{n_rows} labelled signals, {n_events} events in range", "",
             f"{'bucket (min)':<12} {'n':>5} {'events':>6} {'meanR':>7} {'medR':>7}"
             f" {'win%':>5} {'diff':>7} {'t':>6}  note"]
    for b in buckets:
        fmt = lambda v, w, p=2: f"{v:>{w}.{p}f}" if v is not None else " " * (w - 1) + "-"
        lines.append(
            f"{b.label:<12} {b.n:>5} {b.events:>6} {fmt(b.mean_r, 7)} {fmt(b.median_r, 7)}"
            f" {b.win_rate * 100:>5.0f} {fmt(b.diff_vs_none, 7)} {fmt(b.t_vs_none, 6, 1)}"
            f"  {'THIN' if b.thin else ''}")
    lines += ["", "diff = bucket mean R minus the 'none' bucket (no event within 120 min).",
              "THIN = under 30 signals or under 3 distinct events. Signals cluster",
              "around an event, so 'events' is the honest sample size, not 'n'."]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", choices=("reversal", "breakout"), default="reversal")
    ap.add_argument("--since", default="2026-07-20")
    ap.add_argument("--events", help="CSV of extra events: utc,tier,title")
    a = ap.parse_args(argv)

    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()
    rows = (reversal_rows if a.engine == "reversal" else breakout_rows)(since)
    rows = [r for r in rows if r["ts"]]
    if not rows:
        print("no signals in range", file=sys.stderr)
        return 1
    lo = datetime.fromtimestamp(min(r["ts"] for r in rows), tz=timezone.utc)
    hi = datetime.fromtimestamp(max(r["ts"] for r in rows), tz=timezone.utc)
    events = eb.scheduled_events(lo, hi)
    if a.events:
        events = sorted(events + eb.load_events_csv(a.events), key=lambda e: e.when)
    print(render(eb.summarise(rows, events), sum(r["r"] is not None for r in rows),
                 len(events)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
