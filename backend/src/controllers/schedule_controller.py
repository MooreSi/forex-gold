"""The trading schedule: when signals are allowed to execute, and the daily
profit target that halts trading once reached.

Its own module rather than part of trading_controller because that file hit
the 200-line controller ceiling, and the gate that caught it is right about
why: a controller long enough to need sections is holding something a service
should own. These are a distinct concern with a distinct page.

Forwards to backend.src.services.risk.schedule unchanged.
"""
from __future__ import annotations

from backend.src.services.risk import schedule as _schedule

__all__ = [
    "DAY_NAMES", "get_trading_schedule", "set_trading_schedule",
    "is_trading_schedule_enabled", "set_trading_schedule_enabled",
    "get_daily_profit_target", "set_daily_profit_target", "parse_hm",
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


def parse_hm(value: str):
    """Parse "HH:MM", raising on anything else.

    Public here because the schedule page needs to validate what was typed
    before saving it, and was reaching for the service's private _parse_hm to
    do it. Same function, named so a page may legitimately call it.
    """
    return _schedule._parse_hm(value)
