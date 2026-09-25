"""Node and updates — pairing, autostart, restart, and applying a release.

Most of it does not trade. **`/active-trader` does**: it decides which of two
paired nodes may open new positions against the shared MT5 account, and it runs
the stand-down/resume handshake so that the answer is never "both". The
sequence and its failure behaviour live in `services/cluster/handover.py`; this
handler forwards to it and decides nothing itself.

Everything else here can stop the app trading, which is why every action is a
POST that says what it did rather than a setting that changes quietly
underneath.

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
from backend.src.controllers import auth_controller as auth_ctl
from backend.src.controllers import remote_controller as remote_ctl
from backend.src.controllers import remote_node_controller as node_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import sync_controller as sync_ctl
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
    # Take over although the paired VPS cannot be reached, on the operator's
    # confirmation that it is not trading. Only ever for taking over.
    without_peer: bool = False


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
            # What the operator asked for, as opposed to what the OS has.
            # `app.py` reconciles the OS to this on every boot, so the two
            # disagree exactly when the scheduler entry has been lost -- to an
            # OS upgrade or a machine migration -- and that is the one state
            # worth telling somebody about. Reporting only `installed` (until
            # 2026-09-21) rendered it as a plain unticked box.
            "enabled": settings_ctl.get_app_config("auto_restart_enabled") == "1",
            "installed": system_ctl.autostart_is_installed(),
            "armed": system_ctl.autostart_is_armed(),
            "check_interval_secs": system_ctl.AUTOSTART_CHECK_INTERVAL_SECS,
        },
    }


@router.get("/licence")
async def licence() -> dict:
    """Who this install is licensed to, and until when.

    **The key is masked here, not in the browser.** It is a credential like any
    other, and the rule in this layer is that one never leaves the machine
    whole. The first group is enough for the operator to match it against what
    they were sent.
    """
    data = auth_ctl.load_licence() or {}
    raw = str(data.get("licence_key") or "")
    return {
        "email": data.get("email", ""),
        "licence_type": data.get("licence_type", ""),
        "expiry_date": data.get("expiry_date", ""),
        "machine_id": auth_ctl.get_fingerprint(),
        "key_masked": (
            (raw.split("-")[0] if "-" in raw else raw[:8])
            + " - **** - **** - **** - **** - **** - **** - ****"
        ) if raw else "",
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
async def set_active_trader(
    body: ActiveTraderWrite, eng: Any = Depends(engine_dep),
) -> dict:
    """Move trading control between this node and the paired one.

    **Not a flag write.** Between 2026-09-18 and this change it was one, which
    was the most dangerous line in the React port: setting `local` without the
    peer standing down leaves two nodes each believing they own the same MT5
    account, and setting `remote_vps` without stopping the local engines leaves
    them running. Neither is visible on screen; both show up as trades nobody
    placed.

    The handshake, the ordering and the failure behaviour belong to
    `services/cluster/handover.py`. A refusal from it is the operator's answer
    and goes back verbatim.
    """
    try:
        if body.trader == "local" and body.without_peer:
            return await sync_ctl.take_over_without_peer()
        if body.trader == "local":
            return await sync_ctl.take_over_locally()
        return await sync_ctl.hand_back_to_remote(
            # Purely so the answer can say how many keep running to their own
            # SL/TP. Handing back closes nothing.
            open_trades=eng.get_open_trades(),
        )
    except sync_ctl.HandoverRefused as exc:
        raise Refusal(str(exc)) from exc


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
async def update_status(include_summary: bool = True) -> dict:
    """Is there a newer release, and what changed in it.

    This answered 500 on every call where an update was actually available --
    the only time it is asked to do anything. `summarise_changes` was handed
    the whole check dict as its first positional argument (it takes
    `local_sha, remote_sha`), was never awaited, and returns `(bullets,
    error)` where a list of lines was expected. Behind `if available`, so an
    install that was up to date never reached it. Fixed 2026-09-20.

    **The summary is a nicety, never a precondition for updating** -- the
    service says so in its own docstring. It costs a paid model call and can
    fail for reasons that have nothing to do with the release, so a failure
    here reports the update anyway, hands back the reason, and leaves the raw
    commit subjects in `update.commits` for the screen to fall back on.

    `include_summary=false` asks for the check alone. The Node & Updates
    screen's Check button and anything keeping a badge honest only need to
    know whether there IS an update; only the popup that shows the operator
    what they are about to install is worth a model call.
    """
    check = await system_ctl.check_for_update()
    changes: list[str] = []
    changes_error = ""

    if check.get("available") and include_summary:
        try:
            changes, changes_error = await system_ctl.summarise_changes(
                check.get("local_sha", ""), check.get("remote_sha", ""),
            )
        except Exception as exc:
            log.debug("[Update] change summary failed: %s", exc)
            changes, changes_error = [], f"{type(exc).__name__}: {exc}"[:200]

    return {
        "current": system_ctl.app_version(),
        # Which repository and branch this was compared against. Not
        # decoration: this checkout's origin and its running branch are not
        # the ones the constants name, so "up to date" can be true of a
        # branch nobody is running.
        "tracking": await system_ctl.update_tracking(),
        "update": check,
        "changes": list(changes),
        "changes_error": changes_error,
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


@router.post("/stop")
async def stop(eng: Any = Depends(engine_dep)) -> dict:
    """Shut the app down. It does not come back on its own.

    The other half of the NiceGUI header's power button. Nothing here closes a
    position: open trades keep running to their own SL/TP on the broker's side,
    which is what happens when the machine is turned off.
    """
    return {"result": await node_ctl.stop_app(eng)}
