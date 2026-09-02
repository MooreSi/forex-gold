"""Shared polling helper — read off the event loop, render on it.

A `ui.timer` with a synchronous callback runs that callback **on the UI event
loop**. Where the callback does I/O — a DB read, a cache-miss HTTP fetch — the
whole dashboard stalls for its duration. That is the same fault class as the
400-600ms stalls that closing the database boundary was meant to end; a
synchronous timer quietly reopens it one page at a time.

A poll has two halves that belong on different threads, so this helper takes
them separately:

    produce()    the read.   Runs in a worker thread. MUST NOT touch NiceGUI.
    apply(data)  the render. Runs on the event loop, where NiceGUI is safe.

Usage:

    poll(5.0, lambda: news_ctl.get_events(), _render_events)

Three behaviours here are load-bearing and are pinned by tests:

  * **A failed read does not render.** Passing the failure through as None
    blanks a populated panel on one transient error.
  * **A failed read does not stop the timer.** The next tick runs.
  * **Ticks do not overlap.** A read slower than the interval would otherwise
    start a second, third and fourth concurrent read, each holding a DB
    connection, and the queue never drains.

`RuntimeError` from `apply` is expected rather than exceptional: NiceGUI raises
it when the parent slot is gone after the user closes the tab. Every other
render failure is logged — swallowing them is how the 44 silent excepts this
package was cleaned of came about in the first place.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from nicegui import ui

log = logging.getLogger(__name__)

_SENTINEL = object()


def make_tick(produce: Callable[[], Any],
              apply: Callable[[Any], None]) -> Callable:
    """Build the coroutine one timer tick runs.

    Exposed separately from `poll()` so the behaviour can be tested without a
    NiceGUI page, a browser or a running server. The re-entrancy guard is
    per-tick-function, which is what makes each poll independent of the others.
    """
    running = {"now": False}

    async def _tick() -> None:
        if running["now"]:
            # The previous read has not finished. Skipping is correct: the next
            # tick will pick up fresher data than this one would have.
            return
        running["now"] = True
        try:
            data = _SENTINEL
            try:
                data = await asyncio.to_thread(produce)
            except Exception as exc:
                log.warning("Poll read failed (%s: %s) — keeping the last "
                            "rendered values", type(exc).__name__, exc)
            if data is _SENTINEL:
                return
            try:
                apply(data)
            except RuntimeError as exc:
                # Parent slot deleted after page disconnect. Routine.
                log.debug("Poll render skipped, page gone: %s", exc)
            except Exception as exc:
                log.warning("Poll render failed (%s: %s)",
                            type(exc).__name__, exc)
        finally:
            running["now"] = False

    return _tick


def poll(interval_s: float,
         produce: Callable[[], Any],
         apply: Callable[[Any], None],
         **timer_kwargs) -> Optional[Any]:
    """Register a `ui.timer` that reads off the loop and renders on it.

    Returns the timer so the caller can cancel or deactivate it.
    """
    return ui.timer(interval_s, make_tick(produce, apply), **timer_kwargs)
