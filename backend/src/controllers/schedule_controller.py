"""The trading schedule: when signals are allowed to execute, and the daily
profit target that halts trading once reached.

Its own module rather than part of trading_controller because that file hit
the 200-line controller ceiling, and the gate that caught it is right about
why: a controller long enough to need sections is holding something a service
should own. These are a distinct concern with a distinct page.

Forwards to backend.src.services.risk.schedule unchanged.
"""
from __future__ import annotations

from backend.src.services.risk import clock as _clock
from backend.src.services.risk import schedule as _schedule

__all__ = [
    "DAY_NAMES", "get_trading_schedule", "set_trading_schedule",
    "is_trading_schedule_enabled", "set_trading_schedule_enabled",
    "get_daily_profit_target", "set_daily_profit_target",
    "daily_profit_target_state", "daily_profit_target_state_async",
    "resume_past_daily_profit_target",
    "describe_trading_clock", "set_trading_clock_offset",
]

DAY_NAMES = _schedule.DAY_NAMES


def get_trading_schedule(*args, **kwargs):
    return _schedule.get_trading_schedule(*args, **kwargs)


def set_trading_schedule(*args, **kwargs):
    """Rewrite the per-day trading windows. Gates when signals may execute."""
    return _schedule.set_trading_schedule(*args, **kwargs)


def is_trading_schedule_enabled(*args, **kwargs):
    return _schedule.is_trading_schedule_enabled(*args, **kwargs)


def set_trading_schedule_enabled(*args, **kwargs):
    """Turn the whole schedule gate on or off."""
    return _schedule.set_trading_schedule_enabled(*args, **kwargs)


def get_daily_profit_target(*args, **kwargs):
    return _schedule.get_daily_profit_target(*args, **kwargs)


def set_daily_profit_target(*args, **kwargs):
    """The target that halts trading for the day once reached."""
    return _schedule.set_daily_profit_target(*args, **kwargs)


def daily_profit_target_state(*args, **kwargs):
    """What the header badge shows: whether the daily target is holding
    orders right now, and the two figures behind that."""
    return _schedule.daily_profit_target_state(*args, **kwargs)


async def daily_profit_target_state_async(*args, **kwargs):
    """The same, read off the event loop for the shell's 5s poll."""
    return await _schedule.daily_profit_target_state_async(*args, **kwargs)


def resume_past_daily_profit_target(*args, **kwargs):
    """Carry on trading for the rest of today despite the daily target."""
    return _schedule.resume_past_daily_profit_target(*args, **kwargs)


# `parse_hm` was here from the stage-1 restructure, exported so the schedule
# page could check what was typed before saving it. The React port lost that
# caller, and the check moved to where it protects every caller rather than one:
# `set_trading_schedule` validates the whole grid itself (services/risk/
# schedule.py, 2026-09-18), including a schedule forwarded by a paired node.


def describe_trading_clock(*args, **kwargs):
    """The clock the windows above are read against — what it is, and the
    time it currently says."""
    return _clock.describe(*args, **kwargs)


def set_trading_clock_offset(*args, **kwargs):
    """Set the trading clock, or None to follow this machine's own clock."""
    return _clock.set_offset_minutes(*args, **kwargs)
