"""Weekly candles, aggregated from the daily ones the bridge already serves.

There is no W1 in this app. `mt5_bridge._TF_MAP` stops at D1, the fake market's
`TF_SECONDS` stops there too, and Set & Forget's top-down read starts at the
weekly. Teaching the bridge a new timeframe would mean changing the process
that talks to the broker in order to add a read-only screen; aggregating the
dailies does not touch it at all.

**Where the week starts is the whole file.** MT5 stamps its bars as server time
(UTC+3) encoded as if it were a UTC epoch -- the same offset `format.ts` exists
to undo. Gold's week opens around 22:00 UTC on Sunday. Bucket on the raw stamp
with an ISO week and that opening bar lands in the week that has just ENDED: the
newest weekly bar is then built from one candle, the weekly bias is read off
mostly noise, and nothing on the chart looks wrong.

So the stamp is converted back to a real instant first, and weeks run Sunday
00:00 UTC to Saturday 23:59 UTC.
"""
from __future__ import annotations

WEEK_SECONDS = 7 * 86400
# MT5 encodes server time (UTC+3) as a UTC epoch. Same constant as the
# frontend's MT5_UTC_OFFSET_SECONDS, for the same reason.
MT5_UTC_OFFSET_SECONDS = 3 * 3600
# The epoch (1970-01-01) was a Thursday, so a Sunday-start week is four days
# behind it. Without the shift the buckets would turn over on a Thursday, which
# would split every real trading week in half.
_SUNDAY_SHIFT = 4 * 86400


def week_index(ts: float) -> int:
    """Which Sunday-to-Saturday week an MT5 stamp falls in.

    Consecutive weeks are consecutive integers, so the caller can group on this
    without knowing anything about calendars.
    """
    instant = float(ts) - MT5_UTC_OFFSET_SECONDS
    return int((instant + _SUNDAY_SHIFT) // WEEK_SECONDS)


def to_weekly(daily: list[dict]) -> list[dict]:
    """Daily candles folded into weekly ones, oldest first.

    Open is the week's first open, close its last close, high and low the
    extremes across it. The bar is stamped with its FIRST day: stamping it with
    the last would plot each weekly bar a week into the future, which beside
    the daily series reads as the weekly leading price.

    The input is sorted rather than trusted. The bridge returns oldest-first
    today, and a weekly open taken from whichever daily happened to be listed
    first would be wrong only on the day that changes.
    """
    if not daily:
        return []

    buckets: dict[int, list[dict]] = {}
    for c in sorted(daily, key=lambda c: float(c.get("ts") or 0.0)):
        buckets.setdefault(week_index(float(c.get("ts") or 0.0)), []).append(c)

    out: list[dict] = []
    for _, group in sorted(buckets.items()):
        out.append({
            "ts": float(group[0]["ts"]),
            "open": float(group[0]["open"]),
            "high": max(float(c["high"]) for c in group),
            "low": min(float(c["low"]) for c in group),
            "close": float(group[-1]["close"]),
        })
    return out
