"""One shared deal-history fetch for the History panels.

Three panels -- the equity curve, the trade table and the calendar -- each ran a
15-second refresh timer and each called `get_deal_history(...)` on every tick,
independently. Measured 2026-09-10: `/history?days=365` alone ran 4.3 times a
minute at idle, and a single page load produced 388 bridge round-trips in 25
seconds (bugs/030). Every one crosses into the Wine-hosted MT5 bridge.

Keyed by `days`, because the trade table and the calendar ask for whatever
period the user has selected and those are genuinely different queries.

SAFE HERE BECAUSE THESE ARE DISPLAY READS. `monitor_cycle` also reads the
broker, to manage open trades, and serving that from a cache would mean acting
on a stale book -- a money bug rather than a latency one. Nothing outside these
panels uses this.
"""
from __future__ import annotations

import time as _time

_DEAL_CACHE_TTL_S = 60.0
_deal_cache: dict = {}


async def cached_deal_history(bridge, days: int, now=None):
    """`bridge.get_deal_history(days)`, shared for _DEAL_CACHE_TTL_S.

    A failure is NOT cached: caching an error would blank the panel for the
    whole window instead of retrying on the next redraw.
    """
    _now = (now or _time.time)()
    hit = _deal_cache.get(days)
    if hit and (_now - hit[0]) < _DEAL_CACHE_TTL_S:
        return hit[1]
    deals = await bridge.get_deal_history(days) or []
    _deal_cache[days] = (_now, deals)
    return deals
