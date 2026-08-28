"""The EA bridge and EA templates, as the UI needs them.

DELIBERATELY DOES NOT EXPOSE get_instance(). Three pages were calling it and
then working on the live EABridge object directly -- one of them reading
`_ea._last_seen`, a private attribute, to build a status string. Handing the
same object out through a controller would satisfy the import contract while
leaving the boundary exactly where it was, so the operations are exposed
instead and the instance stays on this side.

Two of these reach the EA and change how it trades:

  push_template()       sends a template's values to the running EA
  push_global_config()  sends the global parameter block

Both are the calls the pages were already making, routed rather than altered.
Nothing here decides when to send -- the pages still do -- but a module where
some functions only read status and others change trading behaviour should say
which is which.

Template CRUD (save/delete/import) writes the definitions those pushes are
built from. It does not itself reach the EA.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.broker import ea_bridge as _bridge
from backend.src.services.broker import ea_templates as _templates

__all__ = [
    # EA connection — read-only
    "get_effective_ea_status",
    "ea_is_healthy",
    "ea_seconds_since_last_seen",
    # EA connection — reaches the EA
    "push_template",
    "push_global_config",
    # Template definitions
    "list_ea_templates",
    "get_ea_template",
    "save_ea_template",
    "delete_ea_template",
    "export_templates",
    "import_templates",
    "export_filename",
    "override_for_template",
    "ladder_rr",
    "DEFAULTS",
    "EXPORT_EXTENSION",
    "MAX_TP_LEVELS",
]

DEFAULTS = _templates.DEFAULTS
EXPORT_EXTENSION = _templates.EXPORT_EXTENSION
MAX_TP_LEVELS = _templates.MAX_TP_LEVELS


# ── EA connection: read-only ─────────────────────────────────────────────────

def get_effective_ea_status() -> tuple[bool, str]:
    """(connected, scope) for whichever node is actually trading."""
    return _bridge.get_effective_ea_status()


def ea_is_healthy() -> bool:
    """True only if an EA is connected AND its heartbeat is current.

    Collapses the `instance is None or not instance.is_ea_healthy()` pair the
    pages were writing by hand, so no page needs the instance to ask.
    """
    ea = _bridge.get_instance()
    return ea is not None and ea.is_ea_healthy()


def ea_seconds_since_last_seen() -> Optional[float]:
    """How long since the EA last spoke, or None if it never has this session.

    None covers both "no bridge yet" and "bridge exists but never heard from
    an EA", which is what the diagnostics panel was reading `_ea._last_seen`
    directly to work out.
    """
    import time

    ea = _bridge.get_instance()
    if ea is None or ea._last_seen == 0:
        return None
    return time.time() - ea._last_seen


# ── EA connection: reaches the EA ────────────────────────────────────────────

def push_template(name: str, values: dict) -> bool:
    """Send a template's values to the running EA. Returns False if none is
    connected, in which case nothing was sent and the saved values apply on the
    next signal instead."""
    ea = _bridge.get_instance()
    if ea is None:
        return False
    _bridge.schedule_push_template(ea, name, values)
    return True


async def push_global_config() -> bool:
    """Send the global parameter block to the running EA. False if none is
    connected."""
    ea = _bridge.get_instance()
    if ea is None:
        return False
    await ea.push_global_config()
    return True


# ── Template definitions ─────────────────────────────────────────────────────

def list_ea_templates(*args, **kwargs):
    return _templates.list_ea_templates(*args, **kwargs)


def get_ea_template(*args, **kwargs):
    return _templates.get_ea_template(*args, **kwargs)


def save_ea_template(*args, **kwargs):
    return _templates.save_ea_template(*args, **kwargs)


def delete_ea_template(*args, **kwargs):
    return _templates.delete_ea_template(*args, **kwargs)


def export_templates(*args, **kwargs):
    return _templates.export_templates(*args, **kwargs)


def import_templates(*args, **kwargs):
    return _templates.import_templates(*args, **kwargs)


def export_filename(*args, **kwargs):
    return _templates.export_filename(*args, **kwargs)


def override_for_template(*args, **kwargs):
    return _templates.override_for_template(*args, **kwargs)


def ladder_rr(*args, **kwargs):
    return _templates.ladder_rr(*args, **kwargs)
