"""What fills really cost: spread, slippage and the round trip, measured per
closed trade by broker/tca.py and summarised for the Dashboard. GET only."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.src.controllers import fill_cost_controller as fill_ctl

router = APIRouter(prefix="/api/fills", tags=["fills"])


@router.get("/cost")
async def cost(days: int = Query(14, ge=1, le=365)) -> dict:
    return await fill_ctl.report_async(days)
