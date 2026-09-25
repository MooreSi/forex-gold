"""Set & Forget Auto's switch and status.

Forwards, and nothing else. The scanning, the refusals and the order all live
in `services/setforget/auto.py`; this names the three things the page can ask
of it. Nothing here places an order -- the service's own loop does, started
once from `app.startup`.
"""
from __future__ import annotations

from backend.src.services.setforget import auto as _auto

__all__ = ["is_enabled", "set_enabled", "status"]


def is_enabled() -> bool:
    return _auto.is_enabled()


def set_enabled(on: bool) -> None:
    _auto.set_enabled(on)


def status() -> dict:
    """Whether Auto is on, and what its last scan decided and why."""
    return _auto.status()
