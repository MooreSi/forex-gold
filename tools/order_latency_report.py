"""How long the broker takes to execute, per server, from MetaTrader's own logs.

The 2026-09-24 latency audit found the demo server executing with a median of
0.12 s but a 90th percentile of 8.2 s and a worst case of 43 s -- slow enough
that the EA's 15 s acknowledgement window lapsed on orders it had in fact
placed. Whether the live server behaves the same decides what the fix is.

Every execution in a terminal log ends "done in N ms", and every session
starts "authorized on <server>". This reads those two lines and nothing else.
It places, modifies and closes nothing, and reaches no broker.

Usage:
    python -m tools.order_latency_report                    # the CrossOver bottle's logs
    python -m tools.order_latency_report --logs "C:/Program Files/MetaTrader 5/logs"
    python -m tools.order_latency_report --days 7
"""
from __future__ import annotations

import argparse
from pathlib import Path

# The parser lives in the service so Settings > Latency reads the same
# numbers this prints (docs/todo/006).
from backend.src.services.diagnostics.broker_exec_log import (  # noqa: F401
    CROSSOVER_LOGS, Execution, parse_files, parse_lines, summarise,
)

_DEFAULT_LOGS = Path.home() / CROSSOVER_LOGS


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--logs", type=Path, default=_DEFAULT_LOGS)
    ap.add_argument("--days", type=int, default=0, help="only the newest N daily logs")
    args = ap.parse_args(argv)

    files = sorted(p for p in args.logs.glob("20*.log"))
    if args.days:
        files = files[-args.days:]
    summary = summarise(parse_files(files))
    print(f"{len(files)} log file(s) from {args.logs}\n")
    print(f"{'server':28s} {'n':>6s} {'median':>9s} {'p90':>9s} {'max':>9s} {'>5 s':>6s}")
    for server, s in sorted(summary.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{server:28s} {s['n']:6d} {s['median_ms']/1000:8.2f}s "
              f"{s['p90_ms']/1000:8.2f}s {s['max_ms']/1000:8.2f}s {s['over_5s']:6d}")


if __name__ == "__main__":
    main()
