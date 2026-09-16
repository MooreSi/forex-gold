"""Which dashboard timer stalled the loop, by name.

`docs/todo/bugs/030`. That file's history is one long argument for
attribution before optimisation: the stalls went unexplained from July to
September, and what cracked them was nobody clever, it was somebody
aggregating `WARNING asyncio -- Executing <Task ...> took N seconds` lines
that had been in the log the whole time. The source location in those lines
named two exact panel files, and once named they were fixed.

With the panel re-renders gone (they fell from 11.6 per dashboard-hour on
2026-09-12 to 0.0 on 2026-09-16), `Timer._invoke_callback()` is 84-88% of
what is left. And there the log stops helping. All thirty-odd `ui.timer`
call sites in this package produce the identical line:

    coro=<Timer._invoke_callback() done, defined at .../nicegui/timer.py:107>
    created at backend/src/utils/background_tasks.py:40

The user callback is awaited INSIDE NiceGUI's task, so the coroutine name is
NiceGUI's and the source location is NiceGUI's. There is nothing in the
warning to aggregate.

This module puts the name back, and the way it does so is a correction made
the same afternoon the first version shipped.

**What did not work: timing the callback.** The first version wrapped the
callback in a wall clock and logged anything over the loop monitor's
threshold. That is not a stall. asyncio's `slow_callback_duration` hook fires
inside `Handle._run()` and measures ONE SYNCHRONOUS SLICE, which really is
loop-blocking time; a wall clock around an `async def` includes every
`await`, and during an await the loop is free to run everything else.

Measured within the hour: **twelve `[SlowTimer]` lines against zero
`Timer._invoke_callback` stalls**, with all seven flagged callbacks
`async def`. The instrument was reporting HTTP round trips as though they
were stalls.

**What works: renaming the task.** asyncio keeps the measuring, because it
measures the right thing. This module supplies only the name. NiceGUI creates
a dedicated task per invocation (`nicegui/timer.py:95`,
`asyncio.create_task(self._invoke_callback())`), so `current_task().set_name`
from inside the callback puts the label into the `name=` field of the warning
that was already correct:

    Executing <Task finished name='chart/_refresh_fast (…)'
    coro=<Timer._invoke_callback() ...>> took 0.83 seconds

Per-task, so concurrent timers cannot be confused for one another, and no
registry to keep in step with anything.

The wall-clock line is kept, because a slow refresh is worth knowing about,
but it says `completed in` and says that awaits are included. It answers
"which refresh is slow", not "which one froze the app". Those are different
questions and conflating them is what this section exists to record.

**It measures and it does not act.** No skipping, no debouncing, no
swallowing: a probe that changes behaviour on thirty live dashboard timers
would be a worse bug than the one it is chasing. Return values, arguments and
exceptions pass straight through, a raising callback is still timed, and a
sync callback stays sync (NiceGUI schedules coroutine functions differently,
so wrapping one in the other would change *when* it runs).

`tests/frontend/test_timer_probe.py` holds both halves: the behaviour, and
the rule that no page calls `ui.timer` directly.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Any, Callable, Optional

from nicegui import ui

log = logging.getLogger(__name__)

# Deliberately the same number as `backend.src.utils.loop_monitor`'s
# `_WARN_THRESHOLD_S`, and deliberately a copy of it: the import contract
# `frontend-reaches-the-backend-through-controllers` allows this package
# `backend.src.controllers` and nothing else, and a threshold is not worth a
# controller. `TestTheThresholdMatchesTheOneDoingTheMeasuring` is what stops
# the two copies drifting apart.
SLOW_TIMER_S = 0.40


def _origin(callback: Callable) -> str:
    """Where the callback is DEFINED, which is where it gets fixed.

    The caller's frame would name the `timer(...)` line instead, and for the
    closures this package is full of those are usually the same file but the
    wrong end of it.
    """
    code = getattr(callback, "__code__", None)
    if code is None:
        code = getattr(getattr(callback, "__func__", None), "__code__", None)
    if code is None:
        return ""
    return f"{os.path.basename(code.co_filename)}:{code.co_firstlineno}"


def _label_for(callback: Callable) -> str:
    """A name the aggregation can group on.

    Qualified name plus definition site, because neither alone is enough:
    this package has `_tick`, `refresh` and `<lambda>` several times over,
    and `<lambda>` on its own is no more use in a log than
    `Timer._invoke_callback` was.
    """
    name = (getattr(callback, "__qualname__", None)
            or getattr(callback, "__name__", None)
            or type(callback).__name__)
    origin = _origin(callback)
    return f"{name} ({origin})" if origin else str(name)


def _report(label: str, elapsed_s: float) -> None:
    """Completion latency, explicitly NOT loop-blocking time.

    The wording is asserted by a test. A line that reads as a stall is what
    sent the first read of this wrong.
    """
    if elapsed_s > SLOW_TIMER_S:
        log.warning("[SlowTimer] %s completed in %.3fs "
                    "(wall clock, awaits included)", label, elapsed_s)


def _name_this_task(label: str) -> None:
    """Put the label where asyncio's stall warning will print it.

    Called on entry, before the body: a stall happens INSIDE the callback,
    so a name applied afterwards would miss every event it exists to
    attribute. Outside a task, or outside a running loop, there is nothing
    to name and nothing to complain about -- `timed` is a plain wrapper and
    may be called from anywhere.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is not None:
        task.set_name(label)


def timed(callback: Callable, *, label: Optional[str] = None) -> Callable:
    """The same callback, which says how long it took when it takes too long."""
    name = label or _label_for(callback)

    if asyncio.iscoroutinefunction(callback):
        @functools.wraps(callback)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            _name_this_task(name)
            started = time.perf_counter()
            try:
                return await callback(*args, **kwargs)
            finally:
                # try/finally rather than timing after the call: a callback
                # that fails SLOWLY is the one case worth attributing, and
                # it is the one an `except` would skip.
                _report(name, time.perf_counter() - started)
        return _async

    @functools.wraps(callback)
    def _sync(*args: Any, **kwargs: Any) -> Any:
        _name_this_task(name)
        started = time.perf_counter()
        try:
            return callback(*args, **kwargs)
        finally:
            _report(name, time.perf_counter() - started)
    return _sync


def timer(interval_s: float, callback: Callable, *,
          label: Optional[str] = None, **timer_kwargs) -> Any:
    """`ui.timer`, with the callback named in the log when it stalls.

    Drop-in: every argument behaves as `ui.timer`'s does, and the timer
    object is returned so callers can still cancel or deactivate it.
    """
    return ui.timer(interval_s, timed(callback, label=label), **timer_kwargs)
