"""The trading clock, with the configured offset applied.

`backend/src/utils/trading_clock` holds the arithmetic and knows nothing about
settings -- utils depend on nothing above them. This is the thin layer that
reads `trading_clock_offset_min` and hands the answer over, so every caller
gets the same clock without each one loading settings itself.

Default is the machine's own local time, which is the user's own local time on
the user's own machine. An offset is configured only where the machine is not
where its user is -- a VPS. See utils/trading_clock for the reasoning and
docs/simon-handover/020 for what is still open.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from backend.src.utils.trading_clock import (
    MAX_OFFSET_MIN, SETTING_KEY, configured_offset_minutes,
    local_from_timestamp, local_now, local_timestamp, machine_offset_minutes,
)

__all__ = ["offset_minutes", "effective_offset_minutes", "set_offset_minutes",
           "describe", "now", "from_timestamp", "to_timestamp"]


def _rs() -> dict:
    from backend.src.services.risk.risk_settings_repo import get_risk_settings
    try:
        return get_risk_settings() or {}
    except Exception:
        # Read on the path that decides whether to trade. Falling back to the
        # machine's own clock is what every single-machine install wants
        # anyway, so a settings read failure must not stop the check.
        return {}


def offset_minutes() -> Optional[int]:
    return configured_offset_minutes(_rs())


def now() -> datetime:
    """Now, on the trading clock. The drop-in for a bare `datetime.now()`."""
    return local_now(offset_minutes())


def from_timestamp(epoch: float) -> datetime:
    """A stored epoch time on the trading clock."""
    return local_from_timestamp(epoch, offset_minutes())


def to_timestamp(wall: datetime) -> float:
    """The epoch second for a naive trading-clock time."""
    return local_timestamp(wall, offset_minutes())


def effective_offset_minutes() -> int:
    """This node's trading-clock offset as a definite number.

    `offset_minutes()` returns None for "use the machine's own clock", which is
    the right answer locally and useless to tell someone else. This resolves it,
    so the Mac can report what time it actually is where the user is and the
    VPS can follow.
    """
    configured = offset_minutes()
    return machine_offset_minutes() if configured is None else configured


def set_offset_minutes(offset: Optional[int]) -> None:
    """Set the trading clock, or pass None to follow the machine's own clock.

    Validates here rather than at the reader. The readers run on the path that
    decides whether to trade, and the only thing they can usefully do with a
    nonsense offset is ignore it -- which means the setting silently does
    nothing and the UI happily shows the number you typed. Refusing at the one
    place a human types it is the only point where saying no is any use.
    """
    if offset is not None:
        try:
            whole = int(offset)
        except (TypeError, ValueError):
            raise ValueError(f"trading clock offset must be a whole number of "
                             f"minutes, got {offset!r}")
        if whole != offset:
            raise ValueError(f"trading clock offset must be a whole number of "
                             f"minutes, got {offset!r}")
        if abs(whole) > MAX_OFFSET_MIN:
            raise ValueError(f"trading clock offset {whole} is more than a day "
                             f"from UTC -- that is a typo, not a timezone")
        offset = whole

    from backend.src.services.risk.risk_settings_repo import update_risk_settings
    update_risk_settings({SETTING_KEY: offset})


def _label(offset: int) -> str:
    """"UTC+05:30". Sign belongs to the whole offset, not to the hours: -210
    is UTC-03:30, and formatting the parts independently gets that wrong."""
    sign = "-" if offset < 0 else "+"
    total = abs(offset)
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def describe() -> dict:
    """A one-line summary of the clock in force, for the UI to show.

    Reads through the same functions the schedule gate uses, so the time shown
    on the page and the time the gate acts on cannot disagree. That is the
    point of showing it at all.
    """
    configured = offset_minutes()
    effective = machine_offset_minutes() if configured is None else configured
    return {
        "configured": configured,
        "effective": effective,
        "following_machine": configured is None,
        "label": _label(effective),
        "now": now(),
    }
