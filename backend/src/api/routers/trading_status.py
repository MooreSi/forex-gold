"""The header's trading-status badge.

Its own router because `trading.py` is at the 200-line ceiling, and because
this is one screen element with one read and one action.

Neither endpoint places or closes an order. The badge is a read; the resume
restores the normal state by clearing a hold a human or a guard put in place.
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.api.errors import Refusal
from backend.src.controllers import trading_status_controller as status_ctl

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.get("/status-badge")
async def status_badge() -> dict:
    """What the header shows: whether anything is holding automated entries.

    One read for four mechanisms. They are always displayed together and the
    order between them matters -- a badge reading "Trading Active" while a
    news window is holding every entry is a false all-clear, and that is the
    complaint this indicator exists to answer.
    """
    return status_ctl.trading_status_badge()


@router.post("/resume-all")
async def resume_all() -> dict:
    """Clear whichever hold is in force, and say which ones were cleared.

    Separate from `/api/trading/resume`, which lifts the governor's manual
    pause only.

    The daily profit target, if it was the thing holding entries, is lifted for
    TODAY only -- it returns at the day boundary on its own. The response says
    so rather than letting the screen present it as a setting.
    """
    out = status_ctl.resume_trading_all()
    return {
        **out,
        "note": "The daily profit target, if cleared, is lifted for today only.",
    }


@router.post("/remote/resume-all")
async def resume_all_on_vps() -> dict:
    """The same Resume, for a badge that reports the VPS (owner, 2026-09-29).

    The VPS clears its own holds; this node's are never touched. A route of
    its own, like the remote close, so `/resume-all` can never quietly become
    a forward, or this one a local resume.
    """
    try:
        out = await status_ctl.resume_trading_on_vps()
    except status_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc)) from exc
    return {
        **out,
        "note": "The daily profit target, if cleared, is lifted for today only.",
    }
