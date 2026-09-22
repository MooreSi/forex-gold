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
from backend.src.services.broker import ea_template_presets as _presets
from backend.src.services.broker import ea_templates as _templates

__all__ = [
    "get_effective_ea_status", "ea_build_status", "ea_badge_state",
    "ea_is_healthy", "ea_seconds_since_last_seen", "push_template",
    "ea_template_schema",
    "push_global_config", "list_ea_templates", "get_ea_template",
    "save_ea_template", "delete_ea_template", "rename_ea_template",
    "template_references", "export_templates",
    "import_templates", "export_filename", "override_for_template",
    "ladder_rr", "DEFAULTS", "EXPORT_EXTENSION", "MAX_TP_LEVELS",
    "BUILTIN_PRESET_NAME", "install_builtin_template",
]

DEFAULTS = _templates.DEFAULTS
EXPORT_EXTENSION = _templates.EXPORT_EXTENSION
MAX_TP_LEVELS = _templates.MAX_TP_LEVELS
BUILTIN_PRESET_NAME = _presets.PRESET_NAME


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


def ea_template_schema() -> list:
    """Every editable template field, with its type, default and choices.

    The form on Trading > EA templates renders from this rather than from a
    copy of the field list in TypeScript -- a hundred field types maintained
    in two languages drift the first time one is added to only one of them.
    """
    return _templates.field_schema()


def get_ea_template(*args, **kwargs):
    return _templates.get_ea_template(*args, **kwargs)


def save_ea_template(*args, **kwargs):
    return _templates.save_ea_template(*args, **kwargs)


def delete_ea_template(*args, **kwargs):
    return _templates.delete_ea_template(*args, **kwargs)


def rename_ea_template(*args, **kwargs):
    return _templates.rename_ea_template(*args, **kwargs)


def template_references(*args, **kwargs):
    return _templates.template_references(*args, **kwargs)


def install_builtin_template(*args, **kwargs):
    return _presets.install(*args, **kwargs)


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


def ea_build_status(*args, **kwargs):
    """Is the terminal running a stale EA .ex5? See ea_bridge.ea_build_status."""
    from backend.src.services.broker import ea_bridge as _ea
    return _ea.ea_build_status(*args, **kwargs)


def ea_deploy_report(*args, **kwargs) -> dict:
    """Copy the shipped EA into every MetaTrader on this machine.

    Writes files and nothing else. It cannot place, close or size a trade;
    what it can do is change which rules an ALREADY attached EA manages
    existing positions with, once the terminal reloads the new build.
    """
    from backend.src.services.broker import ea_deploy as _dep
    return _dep.deploy(*args, **kwargs)


def ea_compile(experts_dir, platform=None) -> dict:
    """Compile the EA in one Experts folder. Windows only; see the service."""
    from backend.src.services.broker import ea_deploy as _dep
    return _dep.compile_ea(experts_dir, platform=platform)


def ea_binary_is_shipped() -> bool:
    """Whether this build ships a pre-compiled .ex5 beside the source.

    The difference between "one click" and "one click, then press F7": with a
    binary there is nothing for MetaEditor to do.
    """
    from backend.src.services.broker import ea_deploy as _dep
    return _dep.binary_is_shipped()


def ea_badge_state(*args, **kwargs):
    """Colour/text/tooltip for the EA badge. See ea_bridge.ea_badge_state."""
    from backend.src.services.broker import ea_bridge as _ea
    return _ea.ea_badge_state(*args, **kwargs)
