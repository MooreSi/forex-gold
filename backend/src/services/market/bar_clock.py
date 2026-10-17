"""Bars as an engine should read them: closed, and stamped in UTC.

Two facts about the bridge's `/candles` (MT5 `copy_rates_from_pos(symbol,
tf, 0, count)`), both easy to forget and each already costly once:

1. **Position 0 is the bar still forming, and it comes last.** Its "close"
   is the live price and its high and low are only the range so far. A rule
   that asks how a candle CLOSED cannot be asked of it. Trend PA drops it;
   the Breakout engine did not, so "wicked through the level and closed back
   inside" meant "is back inside at this second" (2026-10-17).
2. **Every stamp is broker time** (New York 17:00 = 00:00, so UTC+3 in the
   US summer and UTC+2 in its winter) written as if it were a UTC epoch.
   Measured 2026-10-17: Friday's last H1 bar read 23:00 although gold shuts
   at 21:00 UTC. Anything that reads an HOUR off a bar stamp -- an Asian
   range, a session open -- gets the wrong hour. `/candles_range` is the
   exception: the bridge converts that one to UTC itself.

The tick carries the same clock (MT5 `tick.time`), so the offset is measured
every cycle from the newest tick, as `trend_pa.service.broker_offset` reads it
off the forming bar, and never assumed: the broker moves it twice a year.

Pure: candles and numbers in, new lists out. The inputs are never modified.
"""
from __future__ import annotations

import math
from typing import Optional

# How old the tick may be. Anything under an hour less the skew leaves exactly
# one whole hour that fits; half an hour keeps well clear of that.
MAX_STALE_S = 1800.0
# The bridge and this process can run on different machines.
_SKEW_S = 60.0
_MAX_OFFSET_S = 14 * 3600


def broker_offset_s(broker_ts: Optional[float], now: float) -> Optional[int]:
    """Whole seconds (a multiple of an hour) the broker clock runs ahead of
    UTC, read off one broker-stamped observation made at or before `now` --
    a tick, or the open of the bar still forming.

    None when it cannot be told: no stamp, or one too old to round to the
    right hour. Callers treat that as having no market data, not as zero.
    """
    if not broker_ts:
        return None
    raw = float(broker_ts) - float(now)
    # The observation was made `age` seconds ago, -skew <= age < MAX_STALE_S,
    # so the offset is the one whole hour in [raw - skew, raw + MAX_STALE_S).
    off = math.ceil((raw - _SKEW_S) / 3600.0) * 3600
    if off - raw >= MAX_STALE_S or abs(off) > _MAX_OFFSET_S:
        return None
    return int(off)


def measured_offset_s(tick, bars, now: float) -> Optional[int]:
    """The broker offset read off `tick`, cross-checked against the newest of
    `bars` (a short timeframe, so its last bar opened minutes ago).

    None when there is no usable tick, or when the newest bar is recent enough
    to judge and says something else: a tick the client stamped with its own
    clock reads "no offset" against bars three hours ahead, and believing it
    would make every recent bar look as though it had not closed yet. A bar
    too old to judge (a quiet bucket) does not veto a fresh tick.
    """
    off = broker_offset_s(getattr(tick, "timestamp", None), now) if tick else None
    if off is None or not bars:
        return off
    seen = broker_offset_s(bars[-1].get("ts"), now)
    return off if seen in (None, off) else None


def utc_stamped(candles, offset_s: int) -> list[dict]:
    """Copies of `candles` with their stamps moved from broker time to UTC.
    The forming bar, if any, is kept."""
    return [{**c, "ts": c["ts"] - offset_s} for c in candles or []]


def utc_closed(candles, bar_s: int, offset_s: int, now: float) -> list[dict]:
    """The bars that had CLOSED by `now`, stamped in UTC.

    Decided by each bar's close time, not by dropping the last element: with
    no tick yet in a new bucket, MT5's last bar is the one that just closed.
    """
    return [c for c in utc_stamped(candles, offset_s) if c["ts"] + bar_s <= now]
