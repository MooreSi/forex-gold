"""How long the broker takes to execute, per server, from MetaTrader's own logs.

The 2026-09-24 latency audit found the demo server executing with a median of
0.12 s but a 90th percentile of 8.2 s and a worst case of 43 s -- slow enough
that the EA's 15 s acknowledgement window lapsed on orders it had in fact
placed. Whether the live server behaves the same decides what the fix is.

Every execution in a terminal log ends "done in N ms", and every session
starts "authorized on <server>". This reads those two lines and nothing else.
It places, modifies and closes nothing, and reaches no broker.

Moved here from `tools/order_latency_report.py` (which keeps its CLI and
imports this) so Settings > Latency can show the broker's hop beside ours.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# The CrossOver install's own logs folder: the path the tool always read.
CROSSOVER_LOGS = ("Library/Application Support/CrossOver/Bottles"
                  "/MetaTrader 5/drive_c/Program Files/MetaTrader 5/logs")

DEFAULT_DAYS = 7

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


def log_dirs(home: Optional[Path] = None) -> list[Path]:
    """Every terminal logs folder on this machine that holds a daily log.

    A terminal's data folder (two above MQL5/Experts) has one, and so does the
    CrossOver install folder the tool has always read.
    """
    from backend.src.services.broker import ea_deploy
    root = Path(home) if home is not None else Path.home()
    candidates = [Path(e).parent.parent / "logs" for e in ea_deploy.experts_dirs(root)]
    candidates.append(root / CROSSOVER_LOGS)
    out = []
    for d in candidates:
        try:
            if d.is_dir() and any(d.glob("20*.log")) and d not in out:
                out.append(d)
        except OSError as e:
            log.debug("[BrokerExecLog] %s unreadable: %s", d, e)
    return out


def summary(days: int = DEFAULT_DAYS, home: Optional[Path] = None) -> dict:
    """The newest `days` daily logs of every terminal, summarised per server."""
    dirs = log_dirs(home)
    rows: list[Execution] = []
    files = 0
    for d in dirs:
        newest = sorted(d.glob("20*.log"))[-days:] if days else sorted(d.glob("20*.log"))
        files += len(newest)
        try:
            rows.extend(parse_files(newest))
        except OSError as e:
            log.debug("[BrokerExecLog] reading %s failed: %s", d, e)
    return {"available": bool(files), "dirs": [str(d) for d in dirs],
            "files": files, "days": days, "servers": summarise(rows)}
