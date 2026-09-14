"""Rebuild a panel container only when its data moved.

`docs/todo/bugs/030`. Eighty-nine per cent of this app's event-loop stalls are
the dashboard refreshing itself, and thirty per cent are two panels calling
`.clear()` on six containers every thirty seconds and building each one again
from scratch -- on a timer, whether or not anything changed. Their database
reads are already off the loop. What blocks is NiceGUI element creation, and
that cannot move to a thread because UI objects are not thread-safe. The only
way down is to stop rebuilding.

## Why the digest is over the whole payload

There was a mechanism for this once: `test_signal/panel_data.change_signature`,
which hand-picked five fields. It was built for the Bounce panel, orphaned when
that panel was deleted, and deleted itself on 2026-09-14. Rebuilding it the
same way would rebuild its weakness with it, and bugs/030 names that weakness
exactly: *"a signature that omits a field leaves a trading screen showing a
stale number, which is worse than a stall the owner can see."*

Add a column to a query, forget to add it to the signature, and the panel
silently stops showing it. Nothing fails; the number is just old.

So nothing is picked here. The digest is taken over the **entire payload the
container renders from**. The property that makes this safe is narrow and
worth stating plainly:

    if the payload is equal, and the render is a pure function of the
    payload, then the rendered output is equal.

The first half is this module's job and is pinned by
`tests/frontend/test_render_cache.py`. **The second half is the caller's**, and
it is the thing to check when wiring a new container in: a render that also
reads the wall clock, an engine's live cache, or a setting is NOT a pure
function of its payload, and gating it here will freeze whatever it reads
outside. Put that value in the payload, or leave the container ungated.

## Off by default, deliberately

bugs/030 says this change wants the owner watching the panel while it is
switched on, not an overnight commit. A `SectionCache()` with no argument
answers "rebuild" to every question, which is today's behaviour to the line.
Turning it on is a setting, and it is his.
"""
from __future__ import annotations

import hashlib
from typing import Any

__all__ = ["SectionCache", "payload_digest", "diff_rendering_enabled",
           "DIFF_RENDERING_KEY"]

# app_config, not an Expert Tunable: those are trading behaviour constants with
# a clamped numeric range, and this is a rendering choice about the owner's own
# screen. Same store and same shape as `sync_server_enabled`.
DIFF_RENDERING_KEY = "panel_diff_rendering"

_UNREADABLE = "\x00unreadable"


def diff_rendering_enabled() -> bool:
    """Whether the owner has switched panel diffing on. Default off.

    bugs/030 asks for this to be turned on while he is watching the panel,
    not committed on overnight, because the failure mode is a stale number on
    a trading screen rather than an error. So the default is today's behaviour
    and the toggle is on the Signal Generator page, next to the panels it
    changes.

    Never raises: read at page build, and a page that will not render because
    a performance preference could not be read is a worse outcome than the
    preference being ignored.
    """
    try:
        from backend.src.controllers import settings_controller as _settings
        return str(_settings.get_app_config(DIFF_RENDERING_KEY) or "0") == "1"
    except Exception:                          # noqa: BLE001
        return False


def _canonical(value: Any) -> Any:
    """A shape whose `repr` is stable for equal data.

    Containers and scalars are handled differently and for different reasons:

      * **Sequence order is kept**, because rows are drawn in order and a
        reordering is a visible change.
      * **Mapping order is not**, because the same mapping built two ways is
        the same data, and rebuilding on it would defeat the point.
      * **Scalars go through `repr` unchanged.** That is deliberate and was
        arrived at by mutation-testing: `repr` already separates every pair
        that renders differently -- `0` from `0.0` from `False` from `None`
        from `"0"`, and `-0.0` from `0.0`, which matters because `_pnl_str`
        prints an explicit sign. It is also stable for `nan`, which does not
        equal itself and would otherwise rebuild a container on every tick.
        The first draft of this file hand-coded all three cases; each one was
        an equivalent mutant, and one of them (folding `-0.0` into `0.0`) was
        an actual defect of exactly the kind this module exists to prevent.

    The `repr` fallback assumes a payload of database rows and plain values,
    which is what every caller passes. An object whose `repr` carries its
    memory address would digest differently per instance -- correct for a
    fresh object each tick, and a rebuild every tick, which is today's
    behaviour rather than a stale screen.
    """
    if isinstance(value, dict):
        return ("d", sorted((str(k), _canonical(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return ("s", [_canonical(v) for v in value])
    if isinstance(value, (set, frozenset)):
        return ("S", sorted(repr(_canonical(v)) for v in value))
    try:
        return repr(value)
    except Exception:                          # noqa: BLE001
        # A payload whose repr raises is a bug somewhere else, but it must not
        # take the panel with it. Falling back to a constant means every such
        # object digests alike, so the container rebuilds when something else
        # in the payload moves and not otherwise. A degraded answer, not a
        # wrong one, and better than an exception inside a refresh that six
        # other containers are queued behind.
        return _UNREADABLE


def payload_digest(payload: Any) -> str:
    """A stable short digest of `payload`.

    No try/except here on purpose. `_canonical` already absorbs the only thing
    that can throw -- a value whose `repr` raises -- and everything downstream
    of it is tuples, lists and strings. A second net around this would be
    unreachable, and unreachable defensive code is how a guard stops guarding
    without anyone noticing. The place safety is actually needed is
    `SectionCache.changed`, which is on the refresh path; it has it, and a test
    holds it.
    """
    return hashlib.blake2b(
        repr(_canonical(payload)).encode("utf-8", "backslashreplace"),
        digest_size=16,
    ).hexdigest()


class SectionCache:
    """Per-container digests for one rendered page.

    One instance per page render, not per process: it holds what THIS browser
    tab has drawn, and two tabs on the same panel draw independently.

    Not thread-safe and does not need to be -- every caller is on the asyncio
    event loop, which is the same reason the rendering it guards cannot be
    moved off it.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self._seen: dict[str, str] = {}

    def changed(self, section: str, payload: Any) -> bool:
        """True when `section` should be rebuilt from `payload`.

        Disabled, always True. Enabled, True on the first call for a section
        and thereafter only when the payload's digest moved.

        **Records the digest before the render runs**, so a caller whose render
        then raises must `forget(section)` in its except branch -- otherwise
        that container is frozen until its data moves again. Every render site
        in this app is already inside a try/except that logs and carries on,
        which is exactly where that call goes.
        """
        if not self.enabled:
            return True
        try:
            digest = payload_digest(payload)
        except Exception:                      # noqa: BLE001
            # Rebuild, and forget what was there. This is the one place in the
            # module where an exception is plausible rather than theoretical:
            # the payload is whatever a repo handed the panel. Failing towards
            # "rebuild" is failing towards today's behaviour, and an exception
            # escaping here would take out the five containers behind it.
            self.forget(section)
            return True
        if self._seen.get(section) == digest:
            return False
        self._seen[section] = digest
        return True

    def forget(self, section: str) -> None:
        """Drop `section`'s digest so the next call rebuilds it."""
        self._seen.pop(section, None)

    def forget_all(self) -> None:
        self._seen.clear()
