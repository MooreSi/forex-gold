"""What fills really cost: spread, slippage and the round trip, measured per
closed trade by broker/tca.py and summarised for the Dashboard. GET only.
Read from the node that trades."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.src.api.errors import Refusal
from backend.src.controllers import fill_cost_controller as fill_ctl

router = APIRouter(prefix="/api/fills", tags=["fills"])


@router.get("/cost")
async def cost(days: int = Query(14, ge=1, le=365)) -> dict:
    try:
        return await fill_ctl.report_async(days)
    except fill_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc), status_code=503) from exc
