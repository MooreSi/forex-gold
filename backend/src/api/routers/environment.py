"""Demo or live: which account the whole app is pointed at.

**The biggest single control in this application.** Every other setting decides
what happens on whichever account is selected; this decides whether that
account holds real money. It was a toggle in the NiceGUI header, the React port
dropped it, and it is here on the owner's explicit instruction (2026-09-18).

Its own module rather than another block on `settings.py`, for the reason the
file-organisation rule gives — but also because a reader looking for "what can
point this app at a live account" should find one file, not a section.

Two guards this layer adds, and both are deliberate:

* **Switching to live needs `confirm: true`.** Nothing else in this API asks
  for that. A mis-click must not be enough to move every engine, every order
  and every number onto real money.
* **The restart is last.** The service writes nothing unless the target
  account's credentials are present, and if the restart then fails the app is
  still correctly pointed — the operator restarts it themselves and nothing is
  half-done.

The sequence, and why its order is the safety property, is in
`services/broker/environment.py`.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.controllers import remote_node_controller as node_ctl
from backend.src.controllers import environment_controller as env_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["environment"])


class EnvironmentWrite(BaseModel):
    environment: str
    # Required for `live`. See the handler.
    confirm: bool = False


@router.get("/environment")
async def environment() -> dict:
    """Which account the app is pointed at, and whether each can be reached.

    Reports the login and server for both so the operator can check they are
    about to switch to the account they meant. Never a password.
    """
    return env_ctl.describe_environments()


@router.put("/environment")
async def set_environment(body: EnvironmentWrite, eng: Any = Depends(engine_dep)) -> dict:
    """**Point the whole app at the demo or the live account.**

    Switching to `live` requires `confirm: true`. Nothing else in this API
    needs that, and this does: every other control decides what happens on
    whichever account is selected, and this one decides whether that account
    holds real money. A mis-click must not be enough.

    The four steps that have to happen together are in
    `services/broker/environment.py`, which does the first three and writes
    nothing if the target's credentials are missing. The restart is the fourth
    and happens here, because every cached handle — the runtime, the bridge,
    the engines — was built against the old account.
    """
    target = (body.environment or "").strip().lower()
    if target == "live" and not body.confirm:
        raise Refusal(
            "Switching to the LIVE account needs an explicit confirmation. "
            "It points every engine, every order and every number in this app "
            "at real money.", status_code=400)

    try:
        result = env_ctl.switch_environment(target)
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc

    # Last, and only once the switch is recorded: if this fails the app is
    # still correctly pointed and the operator can restart it themselves.
    try:
        result["restart"] = await node_ctl.restart_app(eng)
    except Exception as exc:
        log.warning("[settings] switched to %s but could not restart: %s", target, exc)
        result["restart"] = (
            "The switch is saved, but the app could not restart itself. "
            "Restart it before trading.")
    return result


