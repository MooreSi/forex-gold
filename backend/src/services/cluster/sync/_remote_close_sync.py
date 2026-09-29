"""Close a VPS-opened position from the Mac (owner, 2026-09-29).

"i should still be able to close the button on the local node it should send
the command to the vps". Both nodes trade one MT5 account, but a VPS trade's
record -- its ladder legs, its profit sync, its close row -- is in the VPS's
database, so only the VPS can close it properly. The Mac sends the VPS's own
trade id (from the heartbeat, `positions/live_view._remote_row`) and the VPS
runs `close_trade(trade_id, reason)`: the same runtime method, with the same
two positional arguments, that `POST /api/trading/trades/{id}/close` calls.
Nothing about the close itself is reshaped (golden rule 2).

Each request carries a `req_id` the ack echoes, so two closes in flight can
never be answered with each other's result. An ack that never comes is not a
refusal -- the VPS may have closed the position and lost the reply -- and
`remote_control.close_on_peer` says so to the operator.

Pinned by tests/core/test_remote_close_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_CLOSE_TRADE, MSG_CLOSE_TRADE_ACK, make,
)

log = logging.getLogger(__name__)


class ClientRemoteCloseMixin:
    async def request_peer_close(self, trade_id: str, reason: str,
                                 timeout: float = 30.0) -> dict:
        """Ask the VPS to close its trade. Returns its ack: {"result"} or {"error"}.

        Raises ConnectionError with no link (nothing was sent), and
        asyncio.TimeoutError when the VPS never answers (it may have closed)."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        req_id = uuid.uuid4().hex
        waiting = self.__dict__.setdefault("_close_acks", {})
        fut = asyncio.get_running_loop().create_future()
        waiting[req_id] = fut
        try:
            await self._ws.send(json.dumps(make(
                MSG_CLOSE_TRADE, req_id=req_id, trade_id=trade_id, reason=reason)))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            waiting.pop(req_id, None)

    def _on_close_trade_ack(self, msg: dict) -> None:
        fut = self.__dict__.get("_close_acks", {}).get(msg.get("req_id"))
        if fut is not None and not fut.done():
            fut.set_result(msg)


class ServerRemoteCloseMixin:
    async def _handle_close_trade(self, ws, msg: dict) -> None:
        req_id = msg.get("req_id")
        trade_id = msg.get("trade_id")
        runtime = getattr(self, "_main_engine", None)
        if runtime is None:
            reply = {"error": "The VPS's trading runtime is not running. Nothing was closed."}
        elif not trade_id:
            reply = {"error": "No trade id was sent. Nothing was closed."}
        else:
            try:
                # Frozen path: two positionals, as the HTTP route passes them.
                reply = {"result": await runtime.close_trade(trade_id, msg.get("reason"))}
            except Exception as e:
                log.warning("[SyncServer] close of %s requested by the Mac failed: %s",
                            trade_id, e)
                reply = {"error": str(e)}
        await ws.send(json.dumps(make(MSG_CLOSE_TRADE_ACK, req_id=req_id, **reply),
                                 default=str))
