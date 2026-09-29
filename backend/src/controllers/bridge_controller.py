"""Restarting the MT5 bridge from Settings > MT5 (2026-09-29).

Its own controller because `broker_controller` is at its 200-line ceiling, and
"what can restart the bridge from the UI" should be findable by name. One
operation, forwarded unchanged: the restart is the runtime's own
`start_bridge_process`, judged by its `get_bridge_health`.
"""
from __future__ import annotations

from backend.src.services.broker import bridge_process as _bridge_process

__all__ = ["restart_bridge"]


async def restart_bridge(eng) -> dict:
    return await _bridge_process.restart_and_wait(eng)
