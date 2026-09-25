"""Set & Forget "Auto" -- read its status and switch it on or off.

**No endpoint here places anything.** Auto's orders come from its own scan loop
(`services/setforget/auto.run_forever`, started at app startup), which puts
every refusal -- demo only, two a day, one open, the rules, the AI -- in front
of the order. A route that placed one would skip them all. The loop orders
through `open_manual_market_order`, the same call the page's Execute button
reaches, so there is still one order path.

Its own module rather than a route on `setforget.py` because that router pins
its route list to reads and a setting: it is the page that cannot trade.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.controllers import setforget_auto_controller as auto_ctl

router = APIRouter(prefix="/api/trading/setforget/auto", tags=["trading"])


class AutoSwitch(BaseModel):
    enabled: bool


@router.get("")
async def auto_status() -> dict:
    return auto_ctl.status()


@router.put("")
async def switch(body: AutoSwitch) -> dict:
    auto_ctl.set_enabled(body.enabled)
    return auto_ctl.status()
