"""Broker-timestamp and duration formatting for the trade-history views.

Moved verbatim from `controllers/history/controller.py`. Bodies are unchanged,
so `tests/controllers/test_history_controller.py` applies to them without
edits; that they still pass is the proof the move was faithful.

The broker-timestamp handling is the part worth reading before changing
anything. MT5 stores UTC+3 wall-clock as if it were a Unix epoch, so a
timestamp interpreted naively as UTC yields *broker* time -- which is what
`format_broker_ts` deliberately wants for display. `broker_ts_to_uk_date`
subtracts the offset first, because a calendar date has to be a real local
date or the monthly P&L calendar puts trades in the wrong day.

Service-local by the utils rule: only analytics uses these today. It moves to
`src/utils/` the moment a second service needs it, not before.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

__all__ = [
    "UK_TZ", "BROKER_OFFSET",
    "format_broker_ts", "format_duration", "to_date", "broker_ts_to_uk_date",
]

UK_TZ = ZoneInfo("Europe/London")
BROKER_OFFSET = 10800  # broker stores UTC+3 timestamps as-if-UTC


def format_broker_ts(ts) -> str:
    """Format an MT5 broker timestamp for display.

    Read as UTC on purpose: MT5 encodes UTC+3 as a Unix epoch, so interpreting
    it as UTC yields broker time, which is what the table columns show.
    """
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def format_duration(seconds: Optional[float]) -> str:
    """Compact human-readable duration -- "45s", "12m", "2h 15m", "3d 4h".

    Used both for how long a closed trade was held (open->close) and for how
    long a Limit Runner / EA Template grid order sat pending before it filled
    (pending_placed_at->open).
    """
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def to_date(ts) -> Optional[date]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except Exception:
        return None


def broker_ts_to_uk_date(ts) -> Optional[date]:
    """Convert a broker timestamp (UTC+3-stored-as-UTC) to a UK calendar date.

    Unlike format_broker_ts, the offset must be removed here: this feeds the
    monthly P&L calendar, and a date derived from broker time would file trades
    opened late in the UK evening under the following day.
    """
    try:
        real_utc_epoch = float(ts) - BROKER_OFFSET
        return datetime.fromtimestamp(real_utc_epoch, tz=UK_TZ).date()
    except Exception:
        return None
