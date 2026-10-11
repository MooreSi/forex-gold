"""The signal engines, by name, and the two bulk lifecycle operations.

One place, because there were two. `engines_controller` held this table and its
start/stop loops from the stage-1 restructure; `services/cluster/handover.py`
grew an identical copy on 2026-09-18 when the mode toggle came back, and two
copies of "which engines exist and which of them may be bulk-started" is how a
stale entry gets re-introduced in one of them and not the other.

**Names, not positions.** Every sync message carries an engine by name, so
nothing on the wire depends on the order here. Bounce kept an empty slot from
its deletion on 2026-09-14 until 2026-09-28 on the belief that the sync server
bound engines positionally across nodes; it never did -- the position only ever
existed inside one process (tests/services/engines/test_bounce_is_gone_from_the_wire.py).
The order still decides how the Signal Generator tab lists them.

A service rather than a controller because a service needs it: the handover
sequence cannot import a controller, and it is the only caller that matters.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.breakout_signal import breakout_signal_service as _bo_svc
from backend.src.services.reversal_engine import reversal_engine_service as _re_svc
from backend.src.services.trend_pa import service as _tpa_svc

__all__ = [
    "ENGINE_NAMES", "ALL_NAMES", "IMPLEMENTED_NAMES", "SHOWN_NAMES", "canonical", "instance", "all_instances",
    "running", "start_stopped", "stop_running",
]

_ENGINE_SERVICES: dict[str, Any] = {
    "breakout": _bo_svc,
    "reversal": _re_svc,
}

ENGINE_NAMES = tuple(_ENGINE_SERVICES)

# Engines added since, reached by NAME only -- never through `all_instances()`,
# which the sync server's `server_start` unpacks. The sync server finds these
# through `instance()`. Trend PA: 2026-09-29, docs/todo/012.
_NAMED_SERVICES: dict[str, Any] = {
    "trend_pa": _tpa_svc,
}


def _all_services() -> dict:
    """Both tables, read when called -- not merged once at import, so the one
    table stays the one table even when a test swaps `_ENGINE_SERVICES`."""
    return {**_ENGINE_SERVICES, **_NAMED_SERVICES}


# Every name a control may address: the table engines plus the named ones.
# "bounce" is not one of them since 2026-09-28: refused like any unknown name.
ALL_NAMES = tuple(_all_services())

# The table engines this build can run. Equal to ENGINE_NAMES now that Bounce's
# empty slot is gone; derived rather than restated, so a `None` slot put back
# one day still drops out of the screen.
IMPLEMENTED_NAMES = tuple(
    name for name, svc in _ENGINE_SERVICES.items() if svc is not None
)

# What the Signal Generator tab shows: the table engines, then the by-name ones.
SHOWN_NAMES = IMPLEMENTED_NAMES + tuple(
    name for name, svc in _NAMED_SERVICES.items() if svc is not None
)

# Other names an engine arrives under. The sync server's `_sub_engines`, and
# so the VPS heartbeat Settings > Remote Node draws its buttons from, calls the
# Reversal engine "reversal_engine" (2026-09-28).
_ALIASES = {"reversal_engine": "reversal"}


def canonical(name: str) -> str:
    """The registry's name for `name`; unknown names come back unchanged."""
    return _ALIASES.get(name, name)


def _of(svc) -> Any:
    """An engine's live instance, or None -- for an empty slot as much as an
    unbuilt one."""
    return svc.get_instance() if svc is not None else None


def instance(name: str) -> Any:
    """The named engine's live instance."""
    return _of(_all_services()[name])


def all_instances() -> tuple:
    """(breakout, reversal)."""
    return tuple(_of(svc) for svc in _ENGINE_SERVICES.values())


def running() -> dict:
    """Every table engine, built or not, plus each by-name engine that has been
    built. An unbuilt by-name engine has no slot to report; the screen reads
    its absence as "not running", which is what it is."""
    out = {name: bool(getattr(_of(svc), "is_running", False))
           for name, svc in _ENGINE_SERVICES.items()}
    for name, svc in _NAMED_SERVICES.items():
        eng = _of(svc)
        if eng is not None:
            out[name] = bool(getattr(eng, "is_running", False))
    return out


def start_stopped() -> None:
    for svc in _all_services().values():
        eng = _of(svc)
        if eng is not None and not getattr(eng, "is_running", False):
            eng.start()


def stop_running() -> None:
    for svc in _all_services().values():
        eng = _of(svc)
        if eng is not None and getattr(eng, "is_running", False):
            eng.stop()
