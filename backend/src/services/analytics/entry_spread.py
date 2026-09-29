"""The spread a closed trade paid at entry, for the Analysis table's Spread
and Cost columns.

A historical fact, so it is cached for good (`trade_spread_cache`) and only
tickets the cache has never seen are looked up. The NiceGUI History page did
this on every refresh; the React port kept the read and dropped the write, so
from 2026-09-21 every new trade showed no spread and a zero cost (found
2026-09-29, 306 of 750 trades in the 30-day window).

The tick comes from `get_ticks_range`, which is on the runtime facade: the
first tick in the minute from the entry is what the bridge's `/tick_at`
returns (`copy_ticks_from(ts, 1)`), and both read the timestamp as UTC.
Adding `get_tick_at` to the facade instead would raise its baseline.

**Bounded per read.** A cold cache is hundreds of tickets and the table polls,
so one read looks up at most `PER_READ`, newest first, and the rest are filled
by the next polls. A ticket with no tick in its minute is remembered for the
life of the process rather than asked for on every poll. A bridge error ends
this read's lookups and records nothing, so the next poll tries again.

Nothing here places, closes or modifies anything.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.src.services.positions import spread_cache as _cache
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)

__all__ = ["fill"]

PER_READ = 40
CONCURRENCY = 4
_WINDOW_SECS = 60.0
# XAUUSD quotes to the cent: one point is 0.01.
_POINT = 0.01

_misses: set[int] = set()


async def _one(engine: Any, ticket: int, open_ts: float, lots: float) -> dict | None:
    ticks = await engine.get_ticks_range(open_ts, open_ts + _WINDOW_SECS)
    if not ticks:
        _misses.add(ticket)
        return None
    first = min(ticks, key=lambda t: float(t.get("time", 0)))
    price = round(float(first["ask"]) - float(first["bid"]), 5)
    spread = {"spread_price": price,
              "spread_points": round(price / _POINT, 1),
              "spread_cost_usd": round(price * lots * CONTRACT_SIZE, 2)}
    await _cache.cache(ticket, spread["spread_price"], spread["spread_points"],
                       spread["spread_cost_usd"])
    return spread


async def fill(engine: Any, needs: list[tuple[int, float, float]]) -> dict[int, dict]:
    """Look up and cache the entry spread for `(ticket, open_ts, lots)`.

    Returns what it found, keyed by ticket. Never raises: a missing spread is
    a blank cell, not a lost table.
    """
    if not hasattr(engine, "get_ticks_range"):
        return {}
    todo = sorted((n for n in needs if n[0] not in _misses and n[1]),
                  key=lambda n: n[1], reverse=True)[:PER_READ]
    found: dict[int, dict] = {}
    for start in range(0, len(todo), CONCURRENCY):
        batch = todo[start:start + CONCURRENCY]
        results = await asyncio.gather(*(_one(engine, *n) for n in batch),
                                       return_exceptions=True)
        failed = False
        for (ticket, _ts, _lots), res in zip(batch, results):
            if isinstance(res, BaseException):
                failed = True
                log.debug("[entry_spread] %s: %s", ticket, res)
            elif res:
                found[ticket] = res
        if failed:
            break
    return found
