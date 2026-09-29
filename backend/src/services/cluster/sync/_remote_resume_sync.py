"""Resume the VPS's trading from the Mac's header (owner, 2026-09-29).

"i should have a resume trading button on the popup as if it was local". The
holds that stop the VPS's entries -- its circuit breaker, its manual pause,
its daily profit target -- are in the VPS's database, so only the VPS can lift
them. The Mac asks and the VPS runs `trading_status.resume_all()`, the same
function its own header's Resume runs. No order is placed by this; it clears
a hold.

Each request carries a `req_id` the ack echoes, as the remote close does. An
ack that never comes is not a refusal -- the VPS may have resumed and lost the
reply -- and `remote_control.resume_trading_on_peer` says so.

Pinned by tests/core/test_resume_vps_trading_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_RESUME_TRADING, MSG_RESUME_TRADING_ACK, make,
)

log = logging.getLogger(__name__)


class ClientRemoteResumeMixin:
    async def request_peer_resume_trading(self, timeout: float = 20.0) -> dict:
        """Ask the VPS to lift its holds. Returns its ack: {"result"} or {"error"}.

        Raises ConnectionError with no link (nothing was sent), and
        asyncio.TimeoutError when the VPS never answers (it may have resumed)."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        req_id = uuid.uuid4().hex
        waiting = self.__dict__.setdefault("_resume_trading_acks", {})
        fut = asyncio.get_running_loop().create_future()
        waiting[req_id] = fut
        try:
            await self._ws.send(json.dumps(make(MSG_RESUME_TRADING, req_id=req_id)))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            waiting.pop(req_id, None)

    def _on_resume_trading_ack(self, msg: dict) -> None:
        fut = self.__dict__.get("_resume_trading_acks", {}).get(msg.get("req_id"))
        if fut is not None and not fut.done():
            fut.set_result(msg)


class ServerRemoteResumeMixin:
    async def _handle_resume_trading(self, ws, msg: dict) -> None:
        from backend.src.services.risk import trading_status
        try:
            reply = {"result": await db_module.to_db_thread(trading_status.resume_all)}
        except Exception as e:
            log.warning("[SyncServer] resume requested by the Mac failed: %s", e)
            reply = {"error": str(e)}
        await ws.send(json.dumps(make(MSG_RESUME_TRADING_ACK, req_id=msg.get("req_id"),
                                      **reply), default=str))
