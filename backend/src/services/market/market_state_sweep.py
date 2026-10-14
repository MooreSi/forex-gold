"""Write each signal's market state, one UTC day per pass. docs/todo/017 item 4.

The shape of `reversal_engine/xasset_sweep`: the newest day with unmeasured
signals first, so a fresh signal is measured within about a minute of the
research loop's timer, then back through history. One H1 range read per pass
per engine. A day the broker has no bars for is skipped for the life of the
process (the caller owns the set) rather than re-asked every minute.

Engine-agnostic: each engine passes its own "which rows are missing" and
"store these" callables, so neither engine's database is touched by the
other's code. Only candle reads. Nothing here places, closes or modifies a
trade.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from backend.src.services.market import market_state as ms

log = logging.getLogger("market_state")

GOLD = "XAUUSD"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


async def sweep_one_day(bridge: Any,
                        missing: Callable[[], list],
                        store: Callable[[Iterable[tuple[int, str]]], None],
                        skip_days: set, tag: str) -> int:
    """Measure the newest unmeasured day. Returns how many rows were written."""
    if bridge is None:
        return 0
    by_day: dict[str, list[dict]] = {}
    for r in missing():
        day = _day(r["created_at"])
        if day not in skip_days:
            by_day.setdefault(day, []).append(r)
    if not by_day:
        return 0
    day = max(by_day)
    rows = by_day[day]
    t_min = min(float(r["created_at"]) for r in rows)
    t_max = max(float(r["created_at"]) for r in rows)
    bars = await bridge.get_candles_range_for_symbol(
        GOLD, t_min - ms.LOOKBACK_S, t_max, "H1")
    if not bars:
        skip_days.add(day)
        log.info("[%s] no H1 history for %s; skipped for this run", tag, day)
        return 0
    pairs = [(int(r["id"]), json.dumps(ms.snapshot(
        bars, float(r["created_at"]), r.get("direction") or ""))) for r in rows]
    store(pairs)
    log.info("[%s] market state for %d signals on %s (%d days still to do)",
             tag, len(pairs), day, len(by_day) - 1)
    return len(pairs)
