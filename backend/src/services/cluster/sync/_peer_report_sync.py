"""Dashboard reports from the node that trades (owner, 2026-10-02).

"it should always show the trading node". The Fill cost and GEX cards read this
machine's own database, so with the VPS trading they described a node that
places no orders. The Mac now asks the VPS for the same report by name and the
VPS answers from its own database.

Read-only: nothing here places, closes or sizes a trade. The name comes off the
wire, so it is looked up in `peer_reports.REPORTS`, a fixed table, and its
arguments are checked there before anything runs.

Each request carries a `req_id` the ack echoes, as the remote close does. An
older VPS has no handler and never answers; `peer_reports` says so.

Pinned by tests/core/test_peer_report_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_PEER_REPORT, MSG_PEER_REPORT_ACK, make,
)

log = logging.getLogger(__name__)


class ClientPeerReportMixin:
    async def request_peer_report(self, name: str, args: dict, timeout: float = 20.0) -> dict:
        """Ask the VPS for a named report. Returns its ack: {"result"} or {"error"}.

        Raises ConnectionError with no link (nothing was sent), and
        asyncio.TimeoutError when the VPS never answers."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        req_id = uuid.uuid4().hex
        waiting = self.__dict__.setdefault("_peer_report_acks", {})
        fut = asyncio.get_running_loop().create_future()
        waiting[req_id] = fut
        try:
            await self._ws.send(json.dumps(
                make(MSG_PEER_REPORT, req_id=req_id, name=name, args=args)))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            waiting.pop(req_id, None)

    def _on_peer_report_ack(self, msg: dict) -> None:
        fut = self.__dict__.get("_peer_report_acks", {}).get(msg.get("req_id"))
        if fut is not None and not fut.done():
            fut.set_result(msg)


class ServerPeerReportMixin:
    async def _handle_peer_report(self, ws, msg: dict) -> None:
        from backend.src.services.cluster import peer_reports
        try:
            reply = {"result": await peer_reports.run_local(
                msg.get("name"), msg.get("args") or {})}
        except Exception as e:
            log.warning("[SyncServer] report %r asked for by the Mac failed: %s",
                        msg.get("name"), e)
            reply = {"error": str(e)}
        await ws.send(json.dumps(make(MSG_PEER_REPORT_ACK, req_id=msg.get("req_id"),
                                      **reply), default=str))
