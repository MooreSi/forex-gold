"""The Trend PA engine's panel (docs/todo/012). Reads, and one button.

Places nothing. The backtest replays recorded candles and stores what the
strategy would have done; it reads history from the bridge and writes only
to the engine's own database. It runs on the node the panel is showing -- the
VPS's in Remote mode -- so the numbers it produces are the ones the panel then
reads.
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.api.errors import Refusal
from backend.src.controllers import trend_pa_controller as tpa_ctl

router = APIRouter(prefix="/api/engines/trend-pa", tags=["engines"])


@router.get("/report")
async def report() -> dict:
    return await tpa_ctl.report()


@router.post("/backtest")
async def backtest() -> dict:
    try:
        return await tpa_ctl.request_backtest()
    except tpa_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc)) from exc
