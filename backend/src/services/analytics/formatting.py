"""Broker-timestamp and duration formatting for the trade-history views.

Moved verbatim from `controllers/history/controller.py`. Bodies are unchanged,
so `tests/controllers/test_history_controller.py` applies to them without
edits; that they still pass is the proof the move was faithful.

The broker-timestamp handling is the part worth reading before changing
anything. MT5 stores UTC+3 wall-clock as if it were a Unix epoch, so a
timestamp interpreted naively as UTC yields *broker* time -- which is what
`format_broker_ts` deliberately wants for display. `broker_ts_to_local_date`
subtracts the offset first, because a calendar date has to be a real local
date or the monthly P&L calendar puts trades in the wrong day.

"Local" means the user's own day, read off the trading clock. It was a
hardcoded Europe/London until 2026-09-01, which is right for the owner and
wrong for everyone else: a trade closed at 08:00 in Sydney landed on the
previous day's square. Nothing anchored to the London market *session* moved
with it -- the ORB report and its 08:15 email are about when London opens,
not about where the user is.

Service-local by the utils rule: only analytics uses these today. It moves to
`src/utils/` the moment a second service needs it, not before.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from backend.src.utils.trading_clock import local_from_timestamp

__all__ = [
    "BROKER_OFFSET",
    "format_broker_ts", "format_duration", "to_date", "broker_ts_to_local_date",
]

BROKER_OFFSET = 10800  # broker stores UTC+3 timestamps as-if-UTC

# Distinguishes "no offset given, resolve it yourself" from an explicit None,
# which means "this machine's own clock" and is a real answer. They behave the
# same here; they stop behaving the same the moment a caller passes one on.
_UNSET = object()


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


def broker_ts_to_local_date(ts, offset_minutes=_UNSET) -> Optional[date]:
    """Convert a broker timestamp (UTC+3-stored-as-UTC) to the user's own date.

    Unlike format_broker_ts, the offset must be removed here: this feeds the
    monthly P&L calendar, and a date derived from broker time would file trades
    closed late in the evening under the following day.

    `offset_minutes` is for tests and for callers converting a whole table
    against one clock; left alone, the trading clock is asked. Returns None on
    anything unparseable -- this runs per row while building a calendar, and
    one bad timestamp must not take the page down.
    """
    try:
        real_utc_epoch = float(ts) - BROKER_OFFSET
    except (TypeError, ValueError):
        return None
    if offset_minutes is _UNSET:
        from backend.src.services.risk import clock as _clock
        return _clock.from_timestamp(real_utc_epoch).date()
    return local_from_timestamp(real_utc_epoch, offset_minutes).date()
