"""The brain view (docs/todo/008): what the app decided about each recent
signal, which gate decided it, and which gates are holding orders right now.

GET only. It reads what the order paths already record and changes nothing;
the dashboard polls it every couple of seconds while the view is open.
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.src.controllers import brain_controller as brain_ctl

router = APIRouter(prefix="/api/brain", tags=["brain"])


@router.get("")
async def snapshot() -> dict:
    return await brain_ctl.snapshot_async()
