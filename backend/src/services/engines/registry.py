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

__all__ = [
    "ENGINE_NAMES", "instance", "all_instances",
    "running", "start_stopped", "stop_running",
]

_ENGINE_SERVICES: dict[str, Any] = {
    "breakout": _bo_svc,
    "reversal": _re_svc,
}

ENGINE_NAMES = tuple(_ENGINE_SERVICES)


def _of(svc) -> Any:
    """An engine's live instance, or None if it has not been built yet."""
    return svc.get_instance()


def instance(name: str) -> Any:
    """The named engine's live instance."""
    return _of(_ENGINE_SERVICES[name])


def all_instances() -> tuple:
    """(breakout, reversal)."""
    return tuple(_of(svc) for svc in _ENGINE_SERVICES.values())


def running() -> dict:
    return {
        name: bool(getattr(_of(svc), "is_running", False))
        for name, svc in _ENGINE_SERVICES.items()
    }


def start_stopped() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = _of(svc)
        if eng is not None and not getattr(eng, "is_running", False):
            eng.start()


def stop_running() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = _of(svc)
        if eng is not None and getattr(eng, "is_running", False):
            eng.stop()
