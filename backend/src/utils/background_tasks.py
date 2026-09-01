"""Stop fire-and-forget tasks being garbage collected before they run.

`asyncio.create_task` is called 183 times in this app with the result thrown
away -- Telegram alerts, profit syncs, pushes to connected admins. CPython's
documentation is explicit about what that costs:

    Important: Save a reference to the result of this function, to avoid a task
    disappearing mid-execution. The event loop only keeps weak references to
    tasks. A task that isn't referenced elsewhere may get garbage collected at
    any time, even before it's done.

Nothing raises when it happens. The alert simply never arrives, or the P&L
never syncs, and there is no trace of either.

Fixing 183 call sites one at a time would be 183 chances to get one wrong, in
money-path code, for a bug that leaves no evidence. A task factory fixes all of
them in one place -- and every one written afterwards -- through
`loop.set_task_factory`, a documented event-loop API.

What it deliberately does NOT do: change how any task behaves. Results,
exceptions, names, cancellation and `gather` all work exactly as before; the
only difference is that a strong reference is held from creation until
completion. `tests/utils/test_background_tasks.py` asserts each of those,
because this sits underneath every await in the application.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Strong references to in-flight tasks. Entries are removed on completion, so
# this is a keep-alive rather than a registry -- one that never let go would be
# a leak with a nicer name.
_alive: set = set()


def _factory(loop, coro, **kwargs):
    task = asyncio.Task(coro, loop=loop, **kwargs)
    _alive.add(task)
    # discard, not remove: a task can complete before this line on an eagerly
    # started coroutine, in which case the callback has already fired.
    task.add_done_callback(_alive.discard)
    return task


def install(loop: asyncio.AbstractEventLoop) -> None:
    """Hold every task created on `loop` until it finishes. Safe to repeat."""
    if loop.get_task_factory() is _factory:
        return
    loop.set_task_factory(_factory)
    log.info("[tasks] background-task keep-alive installed")


def live_count() -> int:
    """How many tasks are currently held. For tests and diagnostics."""
    return len(_alive)
