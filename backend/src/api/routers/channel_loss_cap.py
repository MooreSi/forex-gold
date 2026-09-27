"""Per-channel daily loss cap.

Never places or closes an order. It decides whether a channel's new automated
entries are ALLOWED once that channel has lost its cap for the day; open
positions are untouched. The write echoes the stored state, and a refused
value comes back with the service's reason, which names the channel.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import channel_loss_cap_controller as cap_ctl

router = APIRouter(prefix="/api/channel-loss-cap", tags=["schedule"])


class CapsWrite(BaseModel):
    """`default_cap` applies to every channel; 0 is off. `overrides` maps a
    channel name to its own cap, and 0 there exempts that channel."""
    default_cap: float
    overrides: dict[str, float] = {}


@router.get("")
async def state() -> dict:
    return await cap_ctl.state_async()


@router.put("")
async def set_caps(body: CapsWrite) -> dict:
    try:
        cap_ctl.set_caps(default_cap=body.default_cap, overrides=body.overrides)
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc
    return await cap_ctl.state_async()
