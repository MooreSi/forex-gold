"""The EA link's round trip, for Settings > Latency (docs/todo/006).

Mixed into EABridge. The EA answers a "ping" with a "pong" and always has
(ForexTraderBridge.mq5); until this, only the EA ever sent one. The time
between the two is the local TCP hop plus how long the EA takes to read its
socket: its 200 ms timer, or longer when OnTick is busy managing trades --
which is exactly the delay an order handed to the EA waits behind.

Diagnostic only. A ping changes nothing the EA manages.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional


class LinkProbeMixin:
    _pong_waiter: Optional[asyncio.Future] = None

    async def ping_ms(self, timeout: float = 3.0) -> dict:
        """{"ok", "ms", "detail"} -- never raises."""
        if not self.is_ea_healthy():
            return {"ok": False, "ms": None, "detail": "EA not connected"}
        fut = asyncio.get_running_loop().create_future()
        self._pong_waiter = fut
        t0 = time.monotonic()
        try:
            if not await self._send({"type": "ping"}):
                return {"ok": False, "ms": None, "detail": "send failed"}
            await asyncio.wait_for(fut, timeout=timeout)
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000.0, 1), "detail": ""}
        except asyncio.TimeoutError:
            return {"ok": False, "ms": None, "detail": f"no answer within {timeout:.0f} s"}
        finally:
            if self._pong_waiter is fut:
                self._pong_waiter = None

    def _on_pong(self) -> None:
        fut = self._pong_waiter
        if fut is not None and not fut.done():
            fut.set_result(True)
