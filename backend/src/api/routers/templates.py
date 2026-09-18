"""EA templates — saved rule sets the MetaTrader EA runs natively.

Their own router rather than a section of `trading.py`, which was at its
200-line ceiling: a router that would exceed it is the signal that the surface
is two domains, not that the ceiling is wrong.

A template fully replaces a channel's normal strategy, so editing one changes
how future trades are managed. None of these endpoints touches an open
position.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.src.api.errors import Refusal
from backend.src.controllers import broker_controller as broker_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/templates", tags=["trading", "templates"])


@router.get("")
async def templates() -> dict:
    return {
        "templates": broker_ctl.list_ea_templates(),
        "builtin": broker_ctl.BUILTIN_PRESET_NAME,
        "ea_connected": broker_ctl.ea_is_healthy(),
        "ea_last_seen_secs": broker_ctl.ea_seconds_since_last_seen(),
    }


@router.get("/{name}")
async def template(name: str) -> dict:
    found = broker_ctl.get_ea_template(name)
    if not found:
        raise Refusal(f"No EA template called {name!r}.", status_code=404)
    return found


@router.put("/{name}")
async def save_template(name: str, body: dict) -> dict:
    """Save a template's values, and push them to a connected EA.

    The push is best-effort and its outcome is reported: `pushed: false` means
    the values are saved and will apply on the next signal, which is a
    different thing from a failed save and must not read as one.
    """
    broker_ctl.save_ea_template(name, body)
    return {
        "template": broker_ctl.get_ea_template(name),
        "pushed": broker_ctl.push_template(name, body),
    }


@router.delete("/{name}")
async def delete_template(name: str) -> dict:
    broker_ctl.delete_ea_template(name)
    return {"templates": broker_ctl.list_ea_templates()}


@router.post("/install-builtin")
async def install_builtin() -> dict:
    """Restore the shipped preset. Overwrites a template of the same name."""
    broker_ctl.install_builtin_template()
    return {"templates": broker_ctl.list_ea_templates()}
