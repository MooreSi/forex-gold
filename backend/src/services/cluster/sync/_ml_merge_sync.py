"""Merging ML training rows over the sync link (docs/todo/026).

The Mac pushes its own labelled rows to the VPS, a frame at a time, and then
asks the VPS to run its benefit check. (It pulls the VPS's rows with the
read-only peer report "ml_history", which needs nothing here.)

Nothing here places, closes or sizes a trade. The rows go to
`ml_imported_rows`, never to a live table; `ml_merge.service.import_rows`
checks the dataset name, the origin and the shape before anything is written.

Each request carries a `req_id` the ack echoes, as the peer report does. An
older VPS has no handler and never answers, so the Mac times out.

Pinned by tests/ml_merge/test_merge_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_ML_HISTORY_PUSH, MSG_ML_MERGE_ACK, MSG_ML_MERGE_EVALUATE, make,
)

log = logging.getLogger(__name__)


class ClientMlMergeMixin:
    async def _ml_merge_request(self, payload: dict, timeout: float) -> dict:
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        req_id = uuid.uuid4().hex
        waiting = self.__dict__.setdefault("_ml_merge_acks", {})
        fut = asyncio.get_running_loop().create_future()
        waiting[req_id] = fut
        try:
            await self._ws.send(json.dumps({**payload, "req_id": req_id}, default=str))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            waiting.pop(req_id, None)

    async def push_ml_rows(self, kind: str, origin_node: str, rows: list,
                           timeout: float = 60.0) -> dict:
        """Send one frame of rows. Returns the ack: {"inserted"} or {"error"}."""
        return await self._ml_merge_request(
            make(MSG_ML_HISTORY_PUSH, kind=kind, origin_node=origin_node, rows=rows), timeout)

    async def request_ml_evaluate(self, timeout: float = 600.0) -> dict:
        """Ask the VPS to run its benefit check. Returns {"result"} or {"error"}."""
        return await self._ml_merge_request(make(MSG_ML_MERGE_EVALUATE), timeout)

    def _on_ml_merge_ack(self, msg: dict) -> None:
        fut = self.__dict__.get("_ml_merge_acks", {}).get(msg.get("req_id"))
        if fut is not None and not fut.done():
            fut.set_result(msg)


class ServerMlMergeMixin:
    async def _handle_ml_history_push(self, ws, msg: dict) -> None:
        from backend.src.services.ml_merge import service
        try:
            reply = {"inserted": await asyncio.to_thread(
                service.import_rows, msg.get("kind"), msg.get("origin_node"),
                msg.get("rows"))}
        except Exception as e:
            log.warning("[SyncServer] training rows from the Mac refused: %s", e)
            reply = {"error": str(e)}
        await ws.send(json.dumps(make(MSG_ML_MERGE_ACK, req_id=msg.get("req_id"), **reply)))

    async def _handle_ml_merge_evaluate(self, ws, msg: dict) -> None:
        """Answers when the check is done, from a task of its own: the check
        fits models for up to a minute, and this connection's receive loop
        also carries the Mac's close and resume commands, which must not
        queue behind it."""
        from backend.src.services.ml_merge import service

        async def _run() -> None:
            try:
                reply = {"result": await service.apply_async()}
            except Exception as e:
                log.warning("[SyncServer] training merge check failed: %s", e)
                reply = {"error": str(e)}
            try:
                await ws.send(json.dumps(
                    make(MSG_ML_MERGE_ACK, req_id=msg.get("req_id"), **reply), default=str))
            except Exception as e:
                log.info("[SyncServer] training merge result not delivered: %s", e)

        tasks = self.__dict__.setdefault("_ml_merge_tasks", set())
        task = asyncio.get_running_loop().create_task(_run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
