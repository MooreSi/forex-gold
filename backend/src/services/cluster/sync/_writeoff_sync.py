"""Write off the VPS's unconfirmed placeholders from the Mac (owner, 2026-09-28).

"need to remove the (+2 unconfirmed: in the VPS database, no broker ticket)
-- these are blocking 2 available slots". The rows live in the VPS's
database and only the VPS can read its own broker, so the Mac asks and the
VPS runs `core_template_placeholder_repair.write_off_unconfirmed`, which
writes off only what its broker has no position and no deal for. Pinned by
tests/core/test_write_off_unconfirmed_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_WRITE_OFF_UNCONFIRMED, MSG_WRITE_OFF_UNCONFIRMED_ACK, make,
)

log = logging.getLogger(__name__)


def describe(result: dict) -> str:
    """The operator's one line: how many went, and each one kept, with why."""
    gone = result.get("written_off") or []
    parts = [f"VPS: wrote off {len(gone)} unconfirmed placeholder"
             f"{'' if len(gone) == 1 else 's'}."]
    for k in result.get("kept") or []:
        parts.append(f"Kept {str(k.get('trade_id', ''))[:8]}: {k.get('reason', '')}.")
    return " ".join(parts)


async def request_peer_write_off(timeout: float = 20.0) -> dict:
    """The Mac's sync client asks; see ClientWriteOffMixin."""
    from backend.src.services.cluster.sync import client as _client
    return await _client.get_instance().request_peer_write_off(timeout=timeout)


class ClientWriteOffMixin:
    async def request_peer_write_off(self, timeout: float = 20.0) -> dict:
        """Ask the VPS to write off its unconfirmed placeholders. Returns its
        {"written_off", "kept", "error"}.

        Raises ConnectionError with no link, and asyncio.TimeoutError when
        the VPS never answers (an older VPS has no handler for this)."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        event = self.__dict__.setdefault("_write_off_ack_event", asyncio.Event())
        event.clear()
        await self._ws.send(json.dumps(make(MSG_WRITE_OFF_UNCONFIRMED)))
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return self.__dict__.get("_last_write_off_ack", {})

    def _on_write_off_ack(self, msg: dict) -> None:
        self._last_write_off_ack = msg
        self.__dict__.setdefault("_write_off_ack_event", asyncio.Event()).set()


class ServerWriteOffMixin:
    async def _handle_write_off_unconfirmed(self, ws, msg: dict) -> None:
        from backend.src.services.positions import core_template_placeholder_repair as repair
        runtime = getattr(self, "_main_engine", None)
        bridge = getattr(runtime, "_bridge", None) if runtime is not None else None
        if bridge is None:
            result = {"written_off": [], "kept": [],
                      "error": "The VPS's trading runtime is not running."}
        else:
            try:
                result = await repair.write_off_unconfirmed(bridge)
            except Exception as e:
                log.error("[SyncServer] write-off requested by the Mac failed: %s", e)
                result = {"written_off": [], "kept": [], "error": str(e)}
        await ws.send(json.dumps(make(MSG_WRITE_OFF_UNCONFIRMED_ACK, **result)))
