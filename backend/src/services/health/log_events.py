"""Live-diagnostics feed: meaningful log events since the last app start.

Moved verbatim out of `frontend/pages/settings.py`, where ~90 lines of file
reading, timestamp parsing and regex classification sat inside `render()` and
were handed to `settings_ctl.run_db(...)` to get off the event loop.

Off-loop dispatch is the whole reason this has an async form: the scan reads
and parses the entire log file, which can run to tens of MB, every 5 seconds.
Doing that synchronously on the event loop blocks the app for as long as it
takes -- the same class of stall as the `sqlite3.connect()` sites.

It runs on the DB worker thread despite touching no database. That thread is
the app's one serialised off-loop lane, and borrowing it keeps a second
thread pool from being introduced for one caller.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from backend.src.db.database import to_db_thread

__all__ = ["since_last_start", "since_last_start_async", "MAX_LINES"]

MAX_LINES = 200

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Event patterns to KEEP in the live log.
_KEEP_RE = re.compile(
    r"\b(ERROR|WARNING|CRITICAL"
    r"|NEW SIGNAL|signal_created|signal.*triggered|signal.*rejected"
    r"|trade.*open|Trade opened|order.*placed|placed.*order"
    r"|trade.*clos|Trade closed|SL hit|TP\d hit|stop.loss"
    r"|bridge.*reconnect|bridge.*offline|bridge.*restart"
    r"|Watchdog|self.heal|Self.heal"
    r"|Telegram.*reconnect|auth.*state"
    r"|engine.*start|engine.*stop|SimulationEngine"
    r"|maintenance|AutoTrading"
    r")\b",
    re.IGNORECASE,
)
# Lines to always suppress regardless of content.
_DROP_RE = re.compile(
    r"(HTTP Request: GET.*(tick|candle|positions|account|health)"
    r"|DEBUG"
    r"|keepalive ping OK"
    r"|refresh_header"
    r"|timer.*tick)",
    re.IGNORECASE,
)


def _parse_ts(line: str) -> float | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def since_last_start() -> list[tuple[str, str]]:
    """[(level, line), ...] for meaningful events since app startup.

    Only ERRORs, WARNINGs and recognised app-event INFO lines are included.
    """
    from backend.src.config import DATA_DIR
    log_base = Path(DATA_DIR) / "forex_trader.log"
    if not log_base.exists():
        return []

    try:
        raw_lines = log_base.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    # The most recent "SimulationEngine started" marks the current run.
    startup_ts = 0.0
    for line in raw_lines:
        if "SimulationEngine started" in line:
            ts = _parse_ts(line)
            if ts is not None:
                startup_ts = ts

    result: list[tuple[str, str]] = []
    for line in raw_lines:
        if not line.strip():
            continue
        # Lines from before the last startup belong to a previous run. An
        # unparseable timestamp is kept rather than dropped: a stack-trace
        # continuation line has no timestamp of its own.
        ts = _parse_ts(line)
        if ts is not None and ts < startup_ts:
            continue
        if _DROP_RE.search(line):
            continue
        if " ERROR " in line or " CRITICAL " in line:
            result.append(("error", line))
        elif " WARNING " in line:
            result.append(("warning", line))
        elif _KEEP_RE.search(line):
            result.append(("event", line))

    return result[-MAX_LINES:]


async def since_last_start_async() -> list[tuple[str, str]]:
    return await to_db_thread(since_last_start)
