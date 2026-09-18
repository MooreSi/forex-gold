"""Trading schedule and the daily profit target.

Neither places an order. Both decide whether the engines are ALLOWED to, which
is why the endpoints echo back what was stored and why the daily-target
override is a POST with its own name rather than a flag on a settings write.

**`resume_past_daily_profit_target` is for today only.** The owner asked for it
on 2026-09-16 precisely so it could not become a permanent switch: it clears at
the day boundary on its own, and a UI that presented it as a setting would be
describing something the backend does not do.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import schedule_controller as schedule_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


class ScheduleWrite(BaseModel):
    schedule: dict


class EnabledWrite(BaseModel):
    enabled: bool


class TargetWrite(BaseModel):
    target: float


class ClockOffsetWrite(BaseModel):
    minutes: int


@router.get("/state")
async def state() -> dict:
    """The whole schedule screen in one read."""
    return {
        "schedule": schedule_ctl.get_trading_schedule(),
        "enabled": schedule_ctl.is_trading_schedule_enabled(),
        "daily_target": schedule_ctl.get_daily_profit_target(),
        "daily_state": await schedule_ctl.daily_profit_target_state_async(),
        # Which clock the windows are measured in. The owner's question on
        # 2026-08-31 (simon-handover/017) was exactly this, and a schedule
        # screen that does not answer it is describing hours in an unknown
        # timezone.
        "clock": schedule_ctl.describe_trading_clock(),
    }


@router.put("/schedule")
async def set_schedule(body: ScheduleWrite) -> dict:
    """Write the 7-day × 4-window grid and read it back.

    The echo matters: the service pads a schedule saved before the fourth
    window existed rather than discarding it, so what is stored and what was
    sent can legitimately differ.
    """
    schedule_ctl.set_trading_schedule(body.schedule)
    return {"schedule": schedule_ctl.get_trading_schedule()}


@router.put("/enabled")
async def set_enabled(body: EnabledWrite) -> dict:
    schedule_ctl.set_trading_schedule_enabled(body.enabled)
    return {"enabled": schedule_ctl.is_trading_schedule_enabled()}


@router.put("/daily-target")
async def set_daily_target(body: TargetWrite) -> dict:
    """The whole-day profit target. 0 disables the gate entirely."""
    if body.target < 0:
        raise Refusal("A profit target cannot be negative.", status_code=400)
    schedule_ctl.set_daily_profit_target(body.target)
    return {"daily_target": schedule_ctl.get_daily_profit_target()}


@router.post("/resume-today")
async def resume_today() -> dict:
    """Let automated entries resume past today's profit target.

    **Today only, by design.** It clears at the day boundary on its own, so the
    response says so and the screen repeats it — presenting this as a setting
    would describe behaviour the backend does not have.
    """
    schedule_ctl.resume_past_daily_profit_target()
    return {
        "daily_state": await schedule_ctl.daily_profit_target_state_async(),
        "note": "Resumed for today only. This clears at the day boundary.",
    }


@router.put("/clock-offset")
async def set_clock_offset(body: ClockOffsetWrite) -> dict:
    """Shift the clock the schedule windows are measured against."""
    schedule_ctl.set_trading_clock_offset(body.minutes)
    return {"clock": schedule_ctl.describe_trading_clock()}
