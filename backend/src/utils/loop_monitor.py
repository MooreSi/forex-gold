"""
Event-loop stall watchdog.

Measures asyncio event-loop responsiveness directly: wakes on a fixed
interval and compares actual elapsed time to expected. Since this app is
single-threaded cooperative asyncio, any coroutine that blocks synchronously
(a slow sqlite3 call, blocking file I/O, CPU-bound work) freezes every other
task on the loop — including Telegram message dispatch. A drift here is the
most direct evidence of "local scheduling delay" independent of Telegram
network transit.

On a stall it dumps the currently-running task names so the cause can be
identified from the log rather than guessed at.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

_CHECK_INTERVAL   = 0.25   # seconds between heartbeats
_WARN_THRESHOLD_S = 0.40   # drift above this logs a warning
_MAX_STALLS       = 200    # ring buffer for the Edge Dashboard

_stalls: list[dict] = []
_task: "asyncio.Task | None" = None


def _record(drift_s: float, task_names: list[str]) -> None:
    _stalls.append({
        "ts": time.time(),
        "drift_ms": round(drift_s * 1000, 1),
        "tasks": task_names[:10],
    })
    if len(_stalls) > _MAX_STALLS:
        del _stalls[: len(_stalls) - _MAX_STALLS]


def _task_names() -> list[str]:
    names = []
    for t in asyncio.all_tasks():
        try:
            coro = t.get_coro()
            names.append(getattr(coro, "__qualname__", str(coro)))
        except Exception:
            pass
    # Dedup while preserving order, most-common-looking first isn't worth
    # the complexity here — a sorted set is enough to spot the culprit.
    return sorted(set(names))


async def _watchdog() -> None:
    loop = asyncio.get_running_loop()
    last = loop.time()
    while True:
        await asyncio.sleep(_CHECK_INTERVAL)
        now   = loop.time()
        drift = (now - last) - _CHECK_INTERVAL
        last  = now
        if drift > _WARN_THRESHOLD_S:
            names = _task_names()
            log.warning(
                "[LoopMonitor] event loop stalled %.0fms (expected %.0fms) — "
                "tasks running: %s",
                drift * 1000, _CHECK_INTERVAL * 1000, ", ".join(names) or "?",
            )
            _record(drift, names)


def start() -> None:
    global _task
    loop = asyncio.get_running_loop()
    # _task_names() below only ever lists every task that currently exists
    # on the loop — which is nearly identical on every single stall (the
    # same ~40 background loops are always "pending") and never actually
    # identifies which one is the blocking culprit. asyncio's own debug
    # mode does that precisely: with slow_callback_duration set, it logs
    # "Executing <Task ... coro=<X() running at file.py:LINE>> took Yms"
    # via the stdlib 'asyncio' logger (which already flows into this app's
    # log file), naming the actual offending coroutine and its exact
    # suspend point instead of a static task-name dump.
    loop.set_debug(True)
    loop.slow_callback_duration = _WARN_THRESHOLD_S
    if _task is None or _task.done():
        _task = asyncio.create_task(_watchdog())
        log.info("[LoopMonitor] stall watchdog started (threshold=%dms, "
                 "asyncio debug mode enabled for slow-callback attribution)",
                  int(_WARN_THRESHOLD_S * 1000))


def recent_stalls(limit: int = 50) -> list[dict]:
    return _stalls[-limit:]


def summary() -> dict:
    if not _stalls:
        return {"n": 0}
    drifts = sorted(s["drift_ms"] for s in _stalls)
    n = len(drifts)
    return {
        "n":      n,
        "p50":    drifts[n // 2],
        "p90":    drifts[int(n * 0.9)],
        "max":    drifts[-1],
        "latest": _stalls[-1],
    }
