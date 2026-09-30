"""The dashboard's daily-goal figure: forwards to services/risk/daily_goal."""
from __future__ import annotations

from backend.src.services.risk import daily_goal as _goal

__all__ = ["daily_goal_progress"]


async def daily_goal_progress(engine, balance, day):
    return await _goal.header_progress(engine, balance, day)
