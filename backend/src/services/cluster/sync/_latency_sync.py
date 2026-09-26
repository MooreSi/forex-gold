"""The VPS leg of Settings > Latency, on the existing ping/pong (docs/todo/006).

The Mac sends MSG_PING with a `probe_id`. The VPS answers twice: at once, a
pong echoing the id -- the Mac times that as the round trip -- and then,
once its own probes have run, a pong carrying the id and its `latency` report.

Deliberately no new message type. An older VPS answers a probe with a plain
pong, which never matches, so the Mac says the VPS needs updating instead of
timing an unrelated ping. An older Mac never sends a probe_id, and a plain
ping is answered exactly as it always was.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_PING, MSG_PONG, make,
)

log = logging.getLogger(__name__)

_OLD_VPS = ("The VPS did not answer the probe. It is probably running an older "
            "version, which answers pings without it -- update the VPS.")

_tasks: set = set()


async def _local_report(engine: Any, reader: Any) -> dict:
    from backend.src.services.diagnostics import latency
    return await latency.local_report(engine, reader)


# ── VPS side ─────────────────────────────────────────────────────────────────

def echo(msg: dict) -> dict:
    """The fields a pong must repeat: the probe id, when there is one."""
    pid = msg.get("probe_id")
    return {"probe_id": pid} if pid else {}


def answer_probe(ws, msg: dict, engine: Any) -> None:
    """Send this node's report for a probe ping, off the dispatch path."""
    pid = msg.get("probe_id")
    if not pid:
        return

    async def _run():
        try:
            # The runtime keeps the reader private; the probe only reads it.
            report = await _local_report(engine, getattr(engine, "_tg_reader", None))
        except Exception as e:
            log.warning("[SyncServer] latency probe failed: %s", e)
            report = {"error": f"{type(e).__name__}: {e}"}
        try:
            await ws.send(json.dumps(make(MSG_PONG, probe_id=pid, latency=report)))
        except Exception as e:
            log.debug("[SyncServer] latency report not delivered: %s", e)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ── Mac side ─────────────────────────────────────────────────────────────────

class ClientLatencyMixin:
    async def probe_peer(self, timeout: float = 20.0) -> dict:
        """{"ok", "rtt_ms", "remote", "detail"}. Never raises."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            return {"ok": False, "rtt_ms": None, "remote": None, "detail": "VPS not connected"}
        pid = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        echoed, reported = loop.create_future(), loop.create_future()
        waiters = self.__dict__.setdefault("_latency_probes", {})
        waiters[pid] = (echoed, reported)
        t0 = time.monotonic()
        try:
            # No clock_offset_min: the server reads its absence as "not
            # reported", so a probe cannot move the VPS's trading clock.
            await self._ws.send(json.dumps(make(MSG_PING, probe_id=pid)))
            try:
                back = await asyncio.wait_for(echoed, timeout=min(timeout, 10.0))
            except asyncio.TimeoutError:
                return {"ok": False, "rtt_ms": None, "remote": None, "detail": _OLD_VPS}
            rtt = round((back - t0) * 1000.0, 1)
            try:
                remote = await asyncio.wait_for(reported, timeout=timeout)
            except asyncio.TimeoutError:
                return {"ok": True, "rtt_ms": rtt, "remote": None,
                        "detail": "The VPS answered, but its own report did not arrive in time."}
            return {"ok": True, "rtt_ms": rtt, "remote": remote, "detail": ""}
        except Exception as e:
            return {"ok": False, "rtt_ms": None, "remote": None, "detail": f"{type(e).__name__}: {e}"}
        finally:
            waiters.pop(pid, None)

    def _on_pong(self, msg: dict) -> None:
        w = self.__dict__.get("_latency_probes", {}).get(msg.get("probe_id"))
        if not w:
            return
        echoed, reported = w
        if "latency" in msg:
            if not reported.done():
                reported.set_result(msg["latency"])
        elif not echoed.done():
            echoed.set_result(time.monotonic())
