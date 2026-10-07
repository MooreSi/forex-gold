"""Scroll-back history for the Broker chart (docs/todo/011 phase 2). Read-only.

The chart asks for the bars before the oldest one it holds. The bridge has no
date-addressed candle read it trusts: `mt5.copy_rates_range` returned bars
hours away from the ones asked for (2026-07-07), so even `_get_candles_range`
fetches by position and filters. This does the same through the engine's
ordinary position read, `get_candles(timeframe, count)`, made wide enough to
reach from now back past `before`, then cut.

Bounded by `MAX_HISTORY_BARS` whatever the user scrolls to. The bridge allows
50,000; 20,000 is 13.9 days of 1m and 69 days of 5m, past the spec's target of
seven days on every timeframe, and keeps one scroll from being a 50,000-bar
read. Past the cap the answer is simply empty: the chart stops loading.
"""
from __future__ import annotations

import time
from typing import Any

MAX_HISTORY_BARS = 20_000
# Weekend and holiday gaps mean fewer bars than wall-clock seconds suggest;
# the slack also absorbs the clock difference between this machine and MT5.
SLACK_BARS = 50

TF_SECONDS: dict[str, int] = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14_400, "D1": 86_400,
}


def bars_needed(now: float, before: float, tf_seconds: int, count: int) -> int:
    """How wide a position read must be to hold `count` bars before `before`."""
    gap = max(0, int((now - before) // tf_seconds))
    return min(gap + count + SLACK_BARS, MAX_HISTORY_BARS)


def older_than(rows: list[dict], before: float, count: int) -> list[dict]:
    """The newest `count` bars strictly before `before`, oldest first, each once.

    The chart prepends this to what it holds, so a bar at `before` itself
    would be drawn twice and an unsorted one breaks lightweight-charts outright.
    """
    by_ts: dict[float, dict] = {}
    for r in rows:
        ts = float(r.get("ts") or 0)
        if 0 < ts < before:
            by_ts[ts] = r
    return [by_ts[ts] for ts in sorted(by_ts)][-count:] if count > 0 else []


async def history_before(engine: Any, mt5_tf: str, before: float, count: int,
                         now: float | None = None) -> list[dict]:
    tf_seconds = TF_SECONDS.get(mt5_tf)
    if tf_seconds is None:
        return []
    width = bars_needed(time.time() if now is None else now, before, tf_seconds, count)
    rows = await engine.get_candles(mt5_tf, width) or []
    return older_than(rows, before, count)
