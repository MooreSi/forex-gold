"""The economic-calendar events this app has seen, kept (docs/todo/023, #9).

The ForexFactory feed publishes the current week only, so the Analysis
calendar cannot ask it what was scheduled last month. Every successful fetch
is merged in here instead, and the calendar reads it back. History therefore
starts on the day this shipped; nothing before it can be recovered.

One JSON file next to the feed cache, `news_calendar.history_file()`. Pure
file I/O on already-normalised events, so `news_calendar` can call it without
this module importing back. High and medium impact only, kept 400 days: the
calendar marks days that mattered, and a year of everything is a large file
read on every month the calendar opens.

Nothing here may raise into a caller that matters. `news_calendar` wraps the
merge, and a read of a missing or corrupt file is an empty history.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

KEEP_IMPACTS = frozenset({"high", "medium"})
_DAY = 86400.0
KEEP_SECONDS = 400 * _DAY
_FIELDS = ("ts", "title", "currency", "impact", "forecast", "previous")


def _read(path: Path) -> list[dict]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        events = blob.get("events") if isinstance(blob, dict) else None
        return [e for e in events if isinstance(e, dict) and "ts" in e] if isinstance(events, list) else []
    except (OSError, ValueError):
        return []


def _key(e: dict) -> tuple:
    return (float(e["ts"]), str(e.get("currency", "")), str(e.get("title", "")))


def merge(path: Path, events: list[dict], now: Optional[float] = None) -> None:
    """Fold one fetch's events into the kept history.

    The fetch is the truth for the span it covers, in whole UTC days from its
    first event's to its last's: anything kept in that span that it no longer
    lists was rescheduled or dropped, and is removed, or a moved release would
    show twice. Whole days, because a release moved past the first or last
    event of the fetch is still inside the week the fetch describes.
    """
    now = time.time() if now is None else now
    fresh = [{k: e.get(k, "") for k in _FIELDS} for e in events
             if str(e.get("impact", "")).lower() in KEEP_IMPACTS]
    kept = _read(path)
    if fresh:
        lo = min(float(e["ts"]) for e in fresh) // _DAY * _DAY
        hi = max(float(e["ts"]) for e in fresh) // _DAY * _DAY + _DAY - 1
        kept = [e for e in kept if not lo <= float(e["ts"]) <= hi]
    merged = {_key(e): e for e in kept + fresh}
    cutoff = now - KEEP_SECONDS
    out = sorted((e for e in merged.values() if float(e["ts"]) >= cutoff), key=_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"events": out}), encoding="utf-8")


def between(path: Path, start_ts: float, end_ts: float) -> list[dict]:
    """Kept events with start_ts <= ts <= end_ts, in time order."""
    return [e for e in _read(path) if start_ts <= float(e["ts"]) <= end_ts]
