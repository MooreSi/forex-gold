"""Restart the VPS from the Mac (owner, 2026-09-26).

"there also needs to be a way to restart the vps within the settings tab to
avoid having to login into the vps". Until now only Telegram (/restartapp)
and Remote Desktop could (simon-handover/043, item 4).

The VPS restarts exactly as /restartapp does, through
`TradingRuntime.restart_app`: it persists the Telegram bot offset, hands a
supervised install back to "Setup & Start FOREX.bat", and ends a headless
process that has no web server to stop. This adds a way to ASK, not a second
way to restart.

Nothing is closed. Open positions keep their SL/TP at the broker, but the VPS
manages none of them until it is back; the Settings button says so before it
sends. Pinned by tests/core/test_restart_vps_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_RESTART_NODE, MSG_RESTART_NODE_ACK, make,
)

log = logging.getLogger(__name__)

# The prefix bot_infra.cmd_restart_app returns when it could not restart.
_FAILED = "Restart failed"


class ClientRestartMixin:
    async def request_peer_restart(self, timeout: float = 10.0) -> dict:
        """Ask the VPS to restart. Returns its {"ok", "note"}.

        Raises ConnectionError with no link, and asyncio.TimeoutError when
        the VPS never answers (an older VPS has no handler for this)."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        event = self.__dict__.setdefault("_restart_ack_event", asyncio.Event())
        event.clear()
        await self._ws.send(json.dumps(make(MSG_RESTART_NODE)))
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return self.__dict__.get("_last_restart_ack", {})

    def _on_restart_ack(self, msg: dict) -> None:
        self._last_restart_ack = msg
        self.__dict__.setdefault("_restart_ack_event", asyncio.Event()).set()


class ServerRestartMixin:
    async def _handle_restart_node(self, ws, msg: dict) -> None:
        runtime = getattr(self, "_main_engine", None)
        if runtime is None or not hasattr(runtime, "restart_app"):
            ok, note = False, "The VPS's trading runtime is not running, so it cannot restart itself."
        else:
            try:
                note = await runtime.restart_app([])
                ok = not str(note).startswith(_FAILED)
            except Exception as e:
                ok, note = False, f"{_FAILED}: {e}"
        if ok:
            log.warning("[SyncServer] restarting at the Mac's request: %s", note)
        else:
            log.error("[SyncServer] the Mac asked for a restart and it failed: %s", note)
        await ws.send(json.dumps(make(MSG_RESTART_NODE_ACK, ok=ok, note=str(note))))
