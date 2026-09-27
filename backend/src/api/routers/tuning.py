"""Breakout tuning experiments (docs/todo/007).

Never places or closes an order. Approving a proposal changes one live
Breakout engine parameter, which can decide whether a breakout signal fires;
that is why approval is a named POST and every write echoes the state. A
refusal comes back in the ledger's own words ("An experiment is already
running. One change at a time.") because the operator can act on it.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import tuning_controller as tuning_ctl

router = APIRouter(prefix="/api/engines/breakout/tuning", tags=["engines"])


class ModeWrite(BaseModel):
    approval_required: bool


@router.get("")
async def state() -> dict:
    return await tuning_ctl.state_async()


@router.post("/{exp_id}/approve")
async def approve(exp_id: int) -> dict:
    try:
        await tuning_ctl.approve_async(exp_id)
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc
    return await tuning_ctl.state_async()


@router.post("/{exp_id}/reject")
async def reject(exp_id: int) -> dict:
    try:
        await tuning_ctl.reject_async(exp_id)
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc
    return await tuning_ctl.state_async()


@router.put("/mode")
async def set_mode(body: ModeWrite) -> dict:
    await tuning_ctl.set_approval_required_async(body.approval_required)
    return await tuning_ctl.state_async()
