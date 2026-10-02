"""The latest GLD GEX snapshot (docs/todo/009) for the Dashboard's GEX card.
Display only: nothing that trades reads it. GET only. Read from the node that
trades."""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.api.errors import Refusal
from backend.src.controllers import gex_controller as gex_ctl

router = APIRouter(prefix="/api/gex", tags=["gex"])


@router.get("/latest")
async def latest() -> dict:
    try:
        return await gex_ctl.report_async()
    except gex_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc), status_code=503) from exc
