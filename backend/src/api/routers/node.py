"""Node and updates — pairing, autostart, restart, and applying a release.

None of it trades. All of it can stop the app trading, which is why every
action here is a POST that says what it did rather than a setting that changes
quietly underneath.

**The sync token is a secret**, and `GET /token` does not return it: it says
whether one exists. Generating a new one returns the plaintext exactly once, so
the operator can copy it into the other node — which is the only moment it is
ever supposed to be readable.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.controllers import remote_controller as remote_ctl
from backend.src.controllers import remote_node_controller as node_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import system_controller as system_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/node", tags=["node"])


class Registration(BaseModel):
    email: str
    nickname: str = ""


class AutostartWrite(BaseModel):
    enabled: bool


class ActiveTraderWrite(BaseModel):
    trader: str


@router.get("/state")
async def state() -> dict:
    """Pairing, licence registration and autostart, in one read."""
    return {
        "version": system_ctl.app_version(),
        "active_trader": settings_ctl.get_active_trader(),
        # Whether a token exists, never the token. See the module docstring.
        "sync_token_set": bool(node_ctl.get_sync_token()),
        "registration": remote_ctl.get_status(),
        "registered_email": remote_ctl.get_stored_email(),
        "autostart": {
            "supported": system_ctl.autostart_is_supported(),
            "installed": system_ctl.autostart_is_installed(),
            "armed": system_ctl.autostart_is_armed(),
            "check_interval_secs": system_ctl.AUTOSTART_CHECK_INTERVAL_SECS,
        },
    }


@router.post("/sync-token")
async def new_sync_token() -> dict:
    """Generate a new pairing token and return it **once**.

    Overwrites the previous one — pairing is a one-off, not a per-restart
    ritual — so the response says so rather than letting the operator discover
    it when the other node stops connecting.
    """
    return {
        "token": node_ctl.generate_sync_token(),
        "note": ("Copy this into the other node now. It replaces any previous "
                 "token, and it is not shown again."),
    }


@router.put("/active-trader")
async def set_active_trader(body: ActiveTraderWrite) -> dict:
    """Which node is actually placing trades."""
    settings_ctl.set_active_trader(body.trader)
    return {"active_trader": settings_ctl.get_active_trader()}


@router.post("/register")
async def register(body: Registration) -> dict:
    """Ask the licence server for this machine to be approved."""
    if not body.email.strip():
        raise Refusal("An email address is needed to register this machine.",
                      status_code=400)
    remote_ctl.request_registration(body.email.strip(), body.nickname.strip())
    return {"registration": remote_ctl.get_status()}


@router.put("/autostart")
async def set_autostart(body: AutostartWrite) -> dict:
    """Start the app when the machine boots."""
    if not system_ctl.autostart_is_supported():
        raise Refusal("Autostart is not supported on this platform.")
    if body.enabled:
        system_ctl.autostart_enable()
    else:
        system_ctl.autostart_disable()
    return {
        "supported": True,
        "installed": system_ctl.autostart_is_installed(),
        "armed": system_ctl.autostart_is_armed(),
    }


@router.get("/update")
async def update_status() -> dict:
    """Is there a newer release, and what changed in it."""
    available = await system_ctl.check_for_update()
    return {
        "current": system_ctl.app_version(),
        "update": available,
        "changes": system_ctl.summarise_changes(available) if available else [],
    }


@router.post("/update/apply")
async def apply_update() -> dict:
    """Download and apply a release. The app restarts itself afterwards."""
    return {"result": await system_ctl.apply_update()}


@router.post("/restart")
async def restart(eng: Any = Depends(engine_dep)) -> dict:
    """Restart the app process.

    Through the runtime, not by killing the process: it holds the bot offset
    that has to be persisted first, and stopping the server without that loses
    it.
    """
    return {"result": await node_ctl.restart_app(eng)}
