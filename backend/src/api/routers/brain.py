"""The brain view (docs/todo/008): what the app decided about each recent
signal, which gate decided it, and which gates are holding orders right now.

GET only. It reads what the order paths already record and changes nothing;
the dashboard polls it every couple of seconds while the view is open.
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.api.errors import Refusal
from backend.src.controllers import brain_controller as brain_ctl

router = APIRouter(prefix="/api/brain", tags=["brain"])


@router.get("")
async def snapshot() -> dict:
    # From the node that trades. An unreachable VPS is said, never replaced by
    # this node's gates (2026-10-21).
    try:
        return await brain_ctl.snapshot_async()
    except brain_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc), status_code=503) from exc
