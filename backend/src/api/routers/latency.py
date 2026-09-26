"""Settings > Latency (docs/todo/006).

Its own router because `settings.py` is at the ceiling. GET is the passive
read the tab polls; POST runs the live probes, which reach Telegram, the
bridge, the EA and the VPS once each -- a button, never a poll. Nothing here
places, modifies or closes an order.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.src.api.deps import engine as engine_dep
from backend.src.api.deps import reader as reader_dep
from backend.src.controllers import latency_controller as latency_ctl

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/latency")
async def latency_snapshot() -> dict:
    """Per-hop numbers from the signals seen since this app started."""
    return latency_ctl.snapshot()


@router.post("/latency/check")
async def latency_check(eng: Any = Depends(engine_dep),
                        rdr: Any = Depends(reader_dep)) -> dict:
    """Every live probe here and, when paired, on the VPS."""
    return await latency_ctl.check(eng, rdr)
