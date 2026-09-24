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
import re
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_LOGS = (Path.home() / "Library/Application Support/CrossOver/Bottles"
                 / "MetaTrader 5/drive_c/Program Files/MetaTrader 5/logs")

_AUTH_RE = re.compile(r"authorized on (.+?) through")
_DONE_RE = re.compile(r"\tTrades\t'\d+': (order|modify|close|position)\b.* done in ([0-9.]+) ms")


@dataclass(frozen=True)
class Execution:
    server: str
    kind: str
    ms: float


def parse_lines(lines, server: str = "unknown") -> list[Execution]:
    """Every finished execution, credited to the server logged in at the time.

    `server` is who was logged in when these lines began -- a session outlives
    midnight, so the login is often in the previous day's file.
    """
    out: list[Execution] = []
    for line in lines:
        auth = _AUTH_RE.search(line)
        if auth:
            server = auth.group(1).strip()
            continue
        done = _DONE_RE.search(line)
        if done:
            out.append(Execution(server, done.group(1), float(done.group(2))))
    return out


def _last_login(lines, server: str) -> str:
    for line in lines:
        auth = _AUTH_RE.search(line)
        if auth:
            server = auth.group(1).strip()
    return server


def parse_files(paths) -> list[Execution]:
    """In date order, carrying the logged-in server from one day to the next.

    MetaTrader writes UTF-16; a file that is not is read as UTF-8.
    """
    rows: list[Execution] = []
    server = "unknown"
    for path in sorted(paths, key=lambda p: Path(p).name):
        raw = Path(path).read_bytes()
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        rows.extend(parse_lines(lines, server))
        server = _last_login(lines, server)
    return rows


def _pct(sorted_ms: list[float], q: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def summarise(rows: list[Execution]) -> dict:
    by_server: dict[str, list[float]] = {}
    for r in rows:
        by_server.setdefault(r.server, []).append(r.ms)
    out = {}
    for server, ms in by_server.items():
        ms.sort()
        out[server] = {
            "n": len(ms), "median_ms": _pct(ms, 0.5), "p90_ms": _pct(ms, 0.9),
            "max_ms": ms[-1], "over_5s": sum(1 for v in ms if v > 5000),
        }
    return out


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
