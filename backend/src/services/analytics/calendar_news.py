"""Kept economic-calendar events, filed by trading day for the Analysis
calendar (docs/todo/023, #9).

An event's `ts` is a real UTC instant. The calendar's days are the trading
clock's, so the day is worked out here with the same clock that files a
trade's close (`risk.clock`); a 23:30 UTC release on a clock two hours ahead
belongs to the next day, beside the trades it moved.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

from backend.src.services.risk import clock as _clock
from backend.src.utils import news_calendar as _nc
from backend.src.utils import news_history as _history


def _day_of(ts: float) -> str:
    return _clock.from_timestamp(ts).date().isoformat()


def _bound(day: str, end: bool) -> float:
    """The epoch second a trading-clock day starts (or ends) at. An end like
    YYYY-MM-31 that is not a real date clamps to the month's last day."""
    y, m, d = (int(x) for x in day.split("-"))
    while True:
        try:
            when = date(y, m, d)
            break
        except ValueError:
            d -= 1
    wall = datetime.combine(when + timedelta(days=1) if end else when, dtime())
    return _clock.to_timestamp(wall) - (1 if end else 0)


def between(start: str, end: str) -> list[dict]:
    path = _nc.history_file()
    if path is None:
        return []
    # A day of slack each way, then filed and filtered by trading day: the
    # clock's offset moves an event across the boundary, never by more.
    events = _history.between(path, _bound(start, False) - 86400, _bound(end, True) + 86400)
    out = []
    for e in events:
        day = _day_of(float(e["ts"]))
        if start <= day <= end:
            out.append({"day": day, "ts": float(e["ts"]), "title": str(e.get("title", "")),
                        "currency": str(e.get("currency", "")),
                        "impact": str(e.get("impact", ""))})
    return out


async def between_async(start: str, end: str) -> list[dict]:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(between, start, end)
