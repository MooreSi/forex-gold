"""Bridge health watchdog: check every 60s, restart on repeated failure.

Moved off the runtime in M4 B9e. The runtime keeps a shell that owns the
asyncio task; this owns what the task does.

get_inhibit_reconnect is a callable for the same reason: the user
can hit Stop Bridge mid-loop, and a captured value would restart a
bridge they deliberately stopped.

`is_running` is a CALLABLE, not a bool. The flag it reads is flipped by
shutdown() while the loop is awaiting, so a captured value would leave the
loop spinning after the app was told to stop.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import asyncio
import time

from backend.src.services.broker.watchdog import bridge_watchdog_check as _bridge_watchdog_check_impl


log = logging.getLogger(__name__)


async def bridge_watchdog_loop(
    bridge: Any,
    is_running: Callable[[], bool],
    get_inhibit_reconnect: Callable[[], bool],
    start_bridge_process: Callable[[], Awaitable[bool]],
) -> None:
    """Check bridge health every 60 s. Restart automatically unless the user
    explicitly stopped it via the Stop Bridge button (inhibit flag set).

    Requires CONSECUTIVE_FAIL_THRESHOLD failed checks in a row before
    restarting — a single failed /health call is not trusted on its own.
    The bridge's HTTP server processes one request at a time; under the
    concurrent polling load from four engines (main + breakout + bounce +
    reversal_engine all hitting /tick, /candles, /positions, /account on their own
    schedules) a slow request ahead of a health check in the queue can
    make that check exceed its 4s timeout with MT5 itself perfectly fine.
    That false positive triggered a real, disruptive bridge restart —
    actually causing the "frequent disconnect/reconnect" it was meant to
    prevent. Two failures 60s apart is no longer explainable by one queued
    request; only then is a real problem plausible enough to act on.
    """
    # 180s, not 30s: a full VPS/OS reboot needs MT5 terminal to cold-start
    # and log into the broker before the bridge can serve ticks — observed
    # taking up to ~150s in practice. With the old 30s wait plus two 60s-
    # apart checks (~90s total patience), every full reboot would cross
    # the failure threshold and trigger a false "bridge offline" restart
    # and alert while MT5 was simply still logging in. This only delays
    # the watchdog's first check; a genuine mid-session outage still gets
    # caught by the normal 60s-interval / 2-consecutive-failure logic
    # below once this initial window has passed.
    await asyncio.sleep(180)

    state = {"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}
    while is_running():
        sleep_for = await _bridge_watchdog_check_impl(
            bridge, state, get_inhibit_reconnect(), start_bridge_process,
        )
        await asyncio.sleep(sleep_for)
