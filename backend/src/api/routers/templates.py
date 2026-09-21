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

from fastapi import APIRouter, Query

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


@router.get("/schema")
async def schema() -> dict:
    """What the fields are: type, default and allowed values for each.

    Declared BEFORE `/{name}`: FastAPI matches in definition order, so the
    other way round a request for the schema is read as a template called
    "schema" and answers 404.
    """
    return {"fields": broker_ctl.ea_template_schema()}


@router.get("/export")
async def export_templates(names: list[str] = Query(default=[])) -> dict:
    """The saved templates as a file the operator can keep or share.

    Declared BEFORE `/{name}` for the same reason `/schema` is: FastAPI
    matches in definition order, so the other way round this reads as a
    request for a template called "export" and answers 404.

    No `names` means every template -- which is what the button does. The
    service reads `None` as "all" and an empty list as "none", so the empty
    query has to become None here or Export All exports nothing.
    """
    return {
        "content": broker_ctl.export_templates(list(names) or None),
        "filename": broker_ctl.export_filename(),
    }


@router.post("/import")
async def import_templates(body: dict) -> dict:
    """Add a file's templates to this install.

    `overwrite` defaults to false, and stays that way here: a shared file must
    never silently replace a locally tuned template. The service validates
    every template in the file before writing any of them, so a file with one
    bad entry imports nothing rather than half of itself -- and a bad file is
    the operator picking the wrong one, which is a refusal that names the
    problem, not a 500.
    """
    content = str(body.get("content") or "")
    if not content.strip():
        raise Refusal("That file is empty — there is nothing to import.")
    try:
        result = broker_ctl.import_templates(
            content, overwrite=bool(body.get("overwrite")))
    except ValueError as exc:
        raise Refusal(str(exc)) from exc
    return {**result, "templates": broker_ctl.list_ea_templates()}


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
