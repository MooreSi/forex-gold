"""The signal engines, by name, and the two bulk lifecycle operations.

One place, because there were two. `engines_controller` held this table and its
start/stop loops from the stage-1 restructure; `services/cluster/handover.py`
grew an identical copy on 2026-09-18 when the mode toggle came back, and two
copies of "which engines exist and which of them may be bulk-started" is how
the empty `bounce` slot gets re-introduced in one of them and not the other.

**The order is part of the contract.** `all_instances()` returns
(breakout, bounce, reversal) in that fixed order because the sync server's
`server_start` binds them positionally, and a paired node on an older build
would see Reversal shift into Bounce's place. Bounce's code was deleted on
2026-09-14; its NAME stays for exactly that reason.

A service rather than a controller because a service needs it: the handover
sequence cannot import a controller, and it is the only caller that matters.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.breakout_signal import breakout_signal_service as _bo_svc
from backend.src.services.reversal_engine import reversal_engine_service as _re_svc

__all__ = [
    "ENGINE_NAMES", "instance", "all_instances", "running",
    "start_stopped", "stop_running",
]

_ENGINE_SERVICES: dict[str, Any] = {
    "breakout": _bo_svc,
    "bounce": None,
    "reversal": _re_svc,
}

ENGINE_NAMES = tuple(_ENGINE_SERVICES)

# Belt and braces: the slot is empty, so the loop would skip it anyway. The
# exclusion keeps the safety property asserted rather than incidental — the day
# something is put back in that slot, this is what stops a mode switch starting
# it.
_NOT_BULK_STARTED = ("bounce",)


def _of(svc) -> Any:
    """An engine, or None -- for an empty slot as much as an unbuilt one."""
    return svc.get_instance() if svc is not None else None


def instance(name: str) -> Any:
    """The named engine's live instance."""
    return _of(_ENGINE_SERVICES[name])


def all_instances() -> tuple:
    """(breakout, bounce, reversal), in the fixed binding order."""
    return tuple(_of(svc) for svc in _ENGINE_SERVICES.values())


def running() -> dict:
    return {
        name: bool(getattr(_of(svc), "is_running", False))
        for name, svc in _ENGINE_SERVICES.items()
    }


def start_stopped() -> None:
    for name, svc in _ENGINE_SERVICES.items():
        if name in _NOT_BULK_STARTED:
            continue
        eng = _of(svc)
        if eng is not None and not getattr(eng, "is_running", False):
            eng.start()


def stop_running() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = _of(svc)
        if eng is not None and getattr(eng, "is_running", False):
            eng.stop()
