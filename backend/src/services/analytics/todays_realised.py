"""Realised profit today, from the same closed-trade rows the Calendar sums.

The dashboard's "Today's Goal" must agree with the Calendar's day square, and
the Calendar is built from MT5 deal history (with fees applied), not from the
local `trades` table. Reading the local table left the goal at $0.00 on a day
the Calendar showed +$2.51: closes that reach MT5 by another route, or via the
VPS, are not always in it.

A day is filed exactly as the Calendar files it (`calendarGrid.tradingDate`):
the close stamp minus the broker's UTC+3, read as a UTC date.

The rows come from `trade_table.closed_trades`, which does several local
queries per call and is fetched at most once per `TTL_S` here: the header that
asks is polled every five seconds.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Optional

from backend.src.services.analytics import formatting as _fmt
from backend.src.services.analytics import trade_table as _trade_table

__all__ = ["for_today", "since", "reset_cache"]

TTL_S = 10.0
_cache: dict = {"at": float("-inf"), "day": None, "value": None}


def realised_on(table: dict, day: date) -> Optional[float]:
    """Sum of the rows' P&L closed on `day`; None when MT5 could not answer."""
    if not table or table.get("error"):
        return None
    total = 0.0
    for row in table.get("rows") or []:
        ts = row.get("close_ts")
        if ts and _fmt.to_date(float(ts) - _fmt.BROKER_OFFSET) == day:
            total += float(row.get("pnl") or 0.0)
    return round(total, 2)


def realised_since(table: dict, since_ts: float) -> Optional[float]:
    """Sum of the rows' P&L closed at or after `since_ts` (a true epoch);
    None when MT5 could not answer.

    The daily goal's window (`risk/daily_goal._window_start`): the broker
    day's start, or the last manual Resume. Rows carry MT5's broker-time
    stamp, so the offset comes off before comparing, exactly as `realised_on`
    files a row to its day.
    """
    if not table or table.get("error"):
        return None
    total = 0.0
    for row in table.get("rows") or []:
        ts = row.get("close_ts")
        if ts and float(ts) - _fmt.BROKER_OFFSET >= since_ts:
            total += float(row.get("pnl") or 0.0)
    return round(total, 2)


def reset_cache() -> None:
    _cache.update(at=float("-inf"), day=None, value=None)


async def for_today(engine: Any, day: date) -> Optional[float]:
    now = time.monotonic()
    if _cache["day"] == day and now - _cache["at"] < TTL_S:
        return _cache["value"]
    try:
        table = await _trade_table.closed_trades(engine, 3)
    except Exception:
        return None
    value = realised_on(table, day)
    _cache.update(at=now, day=day, value=value)
    return value


async def since(engine: Any, since_ts: float) -> Optional[float]:
    """MT5's realised P&L since `since_ts`, or None when it cannot answer.

    What the daily goal halts on (owner, 2026-10-07: the local table showed
    +$39.24 on a day MT5 had at -$41.05, and the goal stopped trading). Two
    days of history cover any window that starts inside the broker day.
    Not cached: the goal's sweep is already throttled.
    """
    try:
        table = await _trade_table.closed_trades(engine, 2)
    except Exception:
        return None
    return realised_since(table, since_ts)
