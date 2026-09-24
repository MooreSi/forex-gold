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

**And the line that was blocking** (2026-09-24). The watchdog runs ON the
loop, so it only learns of a stall once the stall is over, and the task list
it logs is every task in the process, sorted -- "BreakoutEngine._cycle_loop,
..." whatever the cause. The nightly 22:00 stall stayed unattributed for
that reason. A sampler thread now watches the heartbeat from outside the
loop and, once the loop has been silent past the threshold, reads the loop
thread's stack while it is still stuck. The warning carries that stack.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

_CHECK_INTERVAL   = 0.25   # seconds between heartbeats
_WARN_THRESHOLD_S = 0.40   # drift above this logs a warning
_MAX_STALLS       = 200    # ring buffer for the Edge Dashboard

_stalls: list[dict] = []
_task: "asyncio.Task | None" = None

# ── The sampler: where the loop is, while it is stuck ────────────────────────

_SAMPLE_INTERVAL = 0.1     # how often the sampler thread looks at the heartbeat
_STACK_FRAMES    = 8       # project frames kept, innermost last
_REPO_ROOT       = str(Path(__file__).resolve().parents[3])

_beat = 0.0                              # monotonic time of the loop's last wake
_loop_thread_id: "int | None" = None
_captured: "str | None" = None           # stack read during the current stall
_sampler: "threading.Thread | None" = None
_sampler_stop = threading.Event()


def _project_stack(frame) -> str:
    """The loop thread's stack, as the project's own frames.

    The innermost frame is usually `time.sleep`, a sqlite call or numpy --
    where the thread physically is, and not the line worth reading. Keeping
    the repo's frames (and the one library frame they called into) is what
    turns "the loop stalled" into "this line stalled it".
    """
    frames = traceback.extract_stack(frame)
    ours = [f for f in frames
            if f.filename.startswith(_REPO_ROOT) and "/.venv/" not in f.filename]
    if not ours:
        ours = frames
    tail = ours[-_STACK_FRAMES:]
    lines = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in tail]
    if frames and frames[-1] is not tail[-1]:
        inner = frames[-1]
        lines.append(f"-> {Path(inner.filename).name}:{inner.lineno} {inner.name}")
    return " < ".join(reversed(lines))


def _sample_forever() -> None:
    global _captured
    while not _sampler_stop.wait(_SAMPLE_INTERVAL):
        if _loop_thread_id is None or _captured is not None:
            continue
        if time.monotonic() - _beat <= _CHECK_INTERVAL + _WARN_THRESHOLD_S:
            continue
        frame = sys._current_frames().get(_loop_thread_id)
        if frame is not None:
            try:
                _captured = _project_stack(frame)
            except Exception as exc:          # a diagnostic must never raise
                _captured = f"(stack unavailable: {exc})"


def _start_sampler() -> None:
    global _sampler, _loop_thread_id, _beat
    _loop_thread_id = threading.get_ident()
    _beat = time.monotonic()
    if _sampler is not None and _sampler.is_alive():
        return
    _sampler_stop.clear()
    _sampler = threading.Thread(target=_sample_forever, name="loop-stall-sampler",
                                daemon=True)
    _sampler.start()


def stop_sampler() -> None:
    """Stop the sampler thread. The app never needs to; tests do."""
    global _sampler, _captured
    _sampler_stop.set()
    if _sampler is not None:
        _sampler.join(timeout=2)
    _sampler = None
    _captured = None


def _record(drift_s: float, task_names: list[str], stack: str = "") -> None:
    _stalls.append({
        "ts": time.time(),
        "drift_ms": round(drift_s * 1000, 1),
        "tasks": task_names[:10],
        "stack": stack,
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
    global _beat, _captured
    loop = asyncio.get_running_loop()
    last = loop.time()
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(_CHECK_INTERVAL)
        _beat = time.monotonic()
        now   = loop.time()
        drift = (now - last) - _CHECK_INTERVAL
        last  = now
        stack, _captured = _captured, None
        if drift > _WARN_THRESHOLD_S:
            names = _task_names()
            log.warning(
                "[LoopMonitor] event loop stalled %.0fms (expected %.0fms) — "
                "blocked in: %s — tasks running: %s",
                drift * 1000, _CHECK_INTERVAL * 1000, stack or "(not sampled)",
                ", ".join(names) or "?",
            )
            _record(drift, names, stack or "")


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
        _start_sampler()
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
