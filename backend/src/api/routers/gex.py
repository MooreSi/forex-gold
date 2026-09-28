"""The latest GLD GEX snapshot (docs/todo/009) for the Dashboard's GEX card.
Display only: nothing that trades reads it. GET only."""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.controllers import gex_controller as gex_ctl

router = APIRouter(prefix="/api/gex", tags=["gex"])


@router.get("/latest")
async def latest() -> dict:
    return await gex_ctl.report_async()
