"""Tick history for one symbol, asked for and returned in true UTC. Read-only.

Standard library only, like mt5_orders.py: mt5_bridge.py imports this, and on
macOS the bridge runs under Wine's Python, which has none of the app's
dependencies.

Why it exists (docs/todo/reversal-engine/250, 2026-09-30): `copy_ticks_range`
reads the datetimes it is given in the terminal's SERVER-time convention and
stamps the ticks it returns in that same convention. On the VPS that is UTC+3,
so every tick read came from three hours before the window asked for, stamped
as if it were inside it. The 2026-09-03 probe compared the stamps with the
request, which agree with each other in either convention, so it passed. The
audit caught it by price: the unshifted query read gold near 4181 while the
live quote was near 4193. `mt5_bridge._get_candles_range` has corrected the
same convention for bars since 2026-07-07.

The offset is measured, never assumed: the broker's own DST calendar moves it.
It is the last quote's stamp minus the clock, which is only an offset while
quotes are arriving -- with the market shut the quote is stale and the
difference is its age. So a reading is trusted only when it lands within
`_FRESH_S` of a whole half hour (a live quote is seconds old; a stale one is
anything) and within `_MAX_OFFSET_S` of UTC. A trusted reading is remembered,
and an untrusted one falls back to it. With nothing remembered the answer is
None, which every caller already treats as "cannot say".

Known gap: a quote stale by almost exactly a half hour (within `_FRESH_S`)
passes the freshness test. Gold's daily break is one hour, so that needs a
read in two narrow minutes of it with nothing learnt earlier in the process.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("mt5_bridge")

_HALF_HOUR_S = 1800.0
_FRESH_S = 90.0
_MAX_OFFSET_S = 14 * 3600.0

_last_offset: Optional[float] = None


def reset_offset_cache() -> None:
    global _last_offset
    _last_offset = None


def server_offset(mt5: Any, symbol: str, now: float) -> Optional[float]:
    """Seconds to ADD to true UTC to get the terminal's server-time stamps,
    or None when no trustworthy reading exists yet."""
    global _last_offset
    try:
        tick = mt5.symbol_info_tick(symbol)
    except Exception as e:
        log.warning("server_offset: symbol_info_tick error: %s", e)
        tick = None
    if tick is not None:
        raw = float(tick.time) - now
        rounded = round(raw / _HALF_HOUR_S) * _HALF_HOUR_S
        if abs(raw - rounded) <= _FRESH_S and abs(rounded) <= _MAX_OFFSET_S:
            _last_offset = rounded
            return rounded
    return _last_offset


def read_range(mt5: Any, symbol: str, from_ts: float, to_ts: float,
               ensure_connected: Callable[[], bool],
               max_span_s: float) -> Optional[list]:
    """Every tick between two true-UTC timestamps, stamped in true UTC, or
    None when the terminal cannot say."""
    if not ensure_connected():
        return None
    if to_ts - from_ts > max_span_s:
        return None
    offset = server_offset(mt5, symbol, time.time())
    if offset is None:
        log.warning("_get_ticks_range: no trustworthy server-time offset yet "
                    "(market closed since this process started?)")
        return None
    try:
        start = datetime.fromtimestamp(float(from_ts) + offset, tz=timezone.utc)
        end   = datetime.fromtimestamp(float(to_ts) + offset, tz=timezone.utc)
        ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
        if ticks is None:
            return None
        return [
            {"time": float(t["time"]) - offset, "bid": float(t["bid"]), "ask": float(t["ask"]), "last": float(t["last"]), "volume": float(t["volume"]), "flags": int(t["flags"])}
            for t in ticks
        ]
    except Exception as e:
        log.warning("_get_ticks_range error: %s", e)
        return None
