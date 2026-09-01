"""Fire-and-forget tasks must not be collected before they run.

`asyncio.create_task` is used 183 times in this app with the result discarded —
Telegram alerts, profit syncs, pushes to connected admins. CPython's own
documentation is explicit about what that means:

    Important: Save a reference to the result of this function, to avoid a task
    disappearing mid-execution. The event loop only keeps weak references to
    tasks. A task that isn't referenced elsewhere may get garbage collected at
    any time, even before it's done.

So any of those 183 can silently not happen. Nothing raises; the alert simply
never arrives, or the P&L never syncs.

Fixing 183 call sites individually would be 183 chances to get one wrong, in
money-path code. A task factory fixes all of them in one place, including every
one written afterwards, using a documented event-loop API.

The test that matters is `test_a_task_survives_losing_its_last_reference`: it
drops the reference and forces a collection, which is the actual failure being
prevented rather than a proxy for it.
"""
from __future__ import annotations

import asyncio
import gc

import pytest

from backend.src.utils import background_tasks

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_registry():
    """Each async test gets its own event loop from pytest-asyncio, so the
    factory goes with it; only the keep-alive set is shared."""
    background_tasks._alive.clear()
    yield
    background_tasks._alive.clear()


class TestTheFactoryKeepsTasksAlive:

    async def test_a_task_survives_losing_its_last_reference(self):
        """The actual failure being prevented, not a proxy for it."""
        background_tasks.install(asyncio.get_running_loop())
        ran: list = []

        async def _work():
            await asyncio.sleep(0)
            ran.append("done")

        asyncio.create_task(_work())       # reference discarded, as in the app
        gc.collect()                       # the collection that could kill it
        await asyncio.sleep(0.05)

        assert ran == ["done"]

    async def test_the_reference_is_released_once_it_finishes(self):
        """A keep-alive that never lets go is a leak with a nicer name."""
        background_tasks.install(asyncio.get_running_loop())

        async def _work():
            return None

        asyncio.create_task(_work())
        assert background_tasks.live_count() == 1

        await asyncio.sleep(0.05)

        assert background_tasks.live_count() == 0

    async def test_a_task_that_RAISES_is_also_released(self):
        """Otherwise one failing alert retains its task for the life of the
        process, and enough of them is a slow leak."""
        background_tasks.install(asyncio.get_running_loop())

        async def _boom():
            raise RuntimeError("alert failed")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0.05)

        assert background_tasks.live_count() == 0
        assert task.done() and task.exception() is not None

    async def test_a_cancelled_task_is_released(self):
        background_tasks.install(asyncio.get_running_loop())

        async def _slow():
            await asyncio.sleep(30)

        task = asyncio.create_task(_slow())
        task.cancel()
        await asyncio.sleep(0.05)

        assert background_tasks.live_count() == 0


class TestItDoesNotChangeHowTasksBehave:
    """The factory sits under every await in the app. Anything it alters, it
    alters everywhere."""

    async def test_the_result_is_still_returned(self):
        background_tasks.install(asyncio.get_running_loop())

        async def _work():
            return 42

        assert await asyncio.create_task(_work()) == 42

    async def test_an_exception_still_propagates_to_the_awaiter(self):
        background_tasks.install(asyncio.get_running_loop())

        async def _boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await asyncio.create_task(_boom())

    async def test_a_name_is_still_honoured(self):
        """`create_task(..., name=...)` shows up in tracebacks and debug output.

        Note this does NOT depend on the factory: `loop.create_task` applies
        the name *after* the factory returns. Kept as a guard on the observable
        behaviour, but the kwargs passthrough is pinned by the context test
        below -- which is the one that actually goes red when it is dropped.
        """
        background_tasks.install(asyncio.get_running_loop())

        async def _work():
            return None

        task = asyncio.create_task(_work(), name="the-name")
        await task

        assert task.get_name() == "the-name"

    async def test_an_explicit_CONTEXT_is_still_honoured(self):
        """The one kwarg `loop.create_task` really does hand to the factory.

        A factory that dropped it would run the task in a copy of the caller's
        context instead of the one it was given -- so anything carried in a
        ContextVar arrives wrong, silently. Mutation found this: dropping
        `**kwargs` left every other test in this file green.
        """
        import contextvars

        var = contextvars.ContextVar("marker")
        var.set("caller")
        background_tasks.install(asyncio.get_running_loop())

        ctx = contextvars.copy_context()
        ctx.run(var.set, "explicit")
        seen: list = []

        async def _work():
            seen.append(var.get())

        await asyncio.create_task(_work(), context=ctx)

        assert seen == ["explicit"], (
            f"the task ran in the wrong context ({seen}) -- the factory is not "
            f"passing `context` through"
        )

    async def test_gather_still_works(self):
        background_tasks.install(asyncio.get_running_loop())

        async def _n(i):
            return i

        assert await asyncio.gather(*(_n(i) for i in range(5))) == [0, 1, 2, 3, 4]

    async def test_cancellation_still_works(self):
        background_tasks.install(asyncio.get_running_loop())

        async def _slow():
            await asyncio.sleep(30)

        task = asyncio.create_task(_slow())
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


class TestInstallingIsSafeToRepeat:
    async def test_installing_twice_does_not_double_up(self):
        loop = asyncio.get_running_loop()
        background_tasks.install(loop)
        background_tasks.install(loop)

        async def _work():
            return None

        asyncio.create_task(_work())

        assert background_tasks.live_count() == 1


class TestItIsActuallyInstalledAtStartup:
    """A keep-alive nobody installs protects nothing. Structural, because
    `startup()` boots engines, watchdogs and long-lived loops that a test would
    then have to unwind."""

    async def test_startup_installs_it_on_the_running_loop(self):
        import ast
        import pathlib

        import backend.src.app as app_mod

        src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "startup")

        installs = [c for c in ast.walk(fn)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "install"]

        assert installs, (
            "background_tasks.install() is not called at startup -- the "
            "keep-alive is inert and every fire-and-forget task is collectable "
            "again"
        )

    async def test_it_is_installed_before_the_engine_is_built(self):
        """`TradingRuntime(...)` and the task supervisors that follow create
        tasks. Installing after them would leave those unprotected."""
        import ast
        import pathlib

        import backend.src.app as app_mod

        src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "startup")

        install_line = min(
            c.lineno for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "install")
        engine_line = min(
            c.lineno for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "TradingRuntime")

        assert install_line < engine_line, (
            f"the keep-alive is installed at line {install_line}, after the "
            f"engine is built at {engine_line} -- anything it starts in between "
            f"is unprotected"
        )
