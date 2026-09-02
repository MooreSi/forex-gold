"""The shared poll helper: read off the event loop, render on it.

stage2 phase4/030. A `ui.timer` with a synchronous callback runs that callback
on the UI event loop. Where the callback does I/O -- a DB read, a cache-miss
HTTP fetch -- the whole dashboard stalls for its duration. Three sites in this
app do exactly that, which is the same class of fault as the 400-600ms UI
stalls the database boundary was closed to fix.

The helper splits a poll into the two halves that need different threads:

    produce()   the read. Runs in a worker thread; MUST NOT touch NiceGUI.
    apply(data) the render. Runs on the event loop, where NiceGUI is safe.

The tick coroutine is exposed separately from `poll()` so it can be tested
without a NiceGUI page, a browser or a running server.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from frontend.components import poll as poll_mod


def _run(coro_fn, times: int = 1):
    async def _main():
        for _ in range(times):
            await coro_fn()
    asyncio.run(_main())


class TestTheReadRunsOffTheEventLoop:
    def test_produce_runs_in_a_worker_thread(self):
        """The whole point. If this runs on the loop thread, a slow read
        freezes every other page in the app."""
        seen: list = []
        loop_thread = None

        def _produce():
            seen.append(threading.current_thread().ident)
            return "data"

        async def _go():
            nonlocal loop_thread
            loop_thread = threading.current_thread().ident
            await poll_mod.make_tick(_produce, lambda d: None)()

        asyncio.run(_go())

        assert seen and seen[0] != loop_thread

    def test_apply_runs_on_the_event_loop_thread(self):
        """NiceGUI element updates from a worker thread are not safe."""
        seen: list = []
        loop_thread = None

        async def _go():
            nonlocal loop_thread
            loop_thread = threading.current_thread().ident
            await poll_mod.make_tick(
                lambda: "x",
                lambda d: seen.append(threading.current_thread().ident))()

        asyncio.run(_go())

        assert seen and seen[0] == loop_thread

    def test_the_read_result_reaches_the_render(self):
        got: list = []

        _run(poll_mod.make_tick(lambda: {"n": 7}, got.append))

        assert got == [{"n": 7}]


class TestFailuresDoNotStopThePoll:
    def test_a_failing_read_does_not_raise(self):
        """A timer callback that raises is dropped by NiceGUI with a traceback
        in the log and no indication on screen."""
        def _boom():
            raise RuntimeError("db gone")

        _run(poll_mod.make_tick(_boom, lambda d: None))

    def test_a_failing_read_does_not_render(self):
        """Rendering None over good data blanks the panel on one bad read."""
        rendered: list = []

        def _boom():
            raise RuntimeError("db gone")

        _run(poll_mod.make_tick(_boom, rendered.append))

        assert rendered == []

    def test_the_next_tick_still_runs_after_a_failure(self):
        calls = {"n": 0}
        rendered: list = []

        def _produce():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return calls["n"]

        _run(poll_mod.make_tick(_produce, rendered.append), times=2)

        assert rendered == [2]

    def test_a_disconnected_page_is_not_an_error(self):
        """NiceGUI raises RuntimeError when the parent slot is gone after the
        user closes the tab. That is routine, not a fault."""
        def _apply(_d):
            raise RuntimeError("parent slot deleted")

        _run(poll_mod.make_tick(lambda: "x", _apply))

    def test_a_real_render_bug_is_not_hidden(self, caplog):
        """Swallowing everything is how the 44 silent excepts happened. A
        render failure must still be logged."""
        def _apply(_d):
            raise ValueError("bad format string")

        _run(poll_mod.make_tick(lambda: "x", _apply))

        assert any("bad format string" in r.getMessage() or
                   "ValueError" in r.getMessage() for r in caplog.records)


class TestSlowReadsDoNotPileUp:
    def test_a_tick_is_skipped_while_the_previous_one_is_still_running(self):
        """A 10s read on a 2s timer would otherwise start five overlapping
        reads, each holding a DB connection."""
        started = {"n": 0}
        release = asyncio.Event()

        async def _go():
            def _slow():
                started["n"] += 1
                import time
                time.sleep(0.05)
                return "x"

            tick = poll_mod.make_tick(_slow, lambda d: None)
            await asyncio.gather(tick(), tick(), tick())
            release.set()

        asyncio.run(_go())

        assert started["n"] == 1

    def test_the_guard_is_released_after_a_failure(self):
        """A read that raises must not leave the poll permanently wedged."""
        calls = {"n": 0}

        def _produce():
            calls["n"] += 1
            raise RuntimeError("boom")

        _run(poll_mod.make_tick(_produce, lambda d: None), times=2)

        assert calls["n"] == 2


class TestTheTimerWiring:
    def test_poll_registers_a_timer_at_the_requested_interval(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(poll_mod.ui, "timer",
                            lambda i, cb, **kw: seen.append((i, kw)) or "timer")

        poll_mod.poll(5.0, lambda: None, lambda d: None)

        assert seen and seen[0][0] == 5.0

    def test_poll_returns_the_timer_so_a_page_can_cancel_it(self, monkeypatch):
        monkeypatch.setattr(poll_mod.ui, "timer", lambda i, cb, **kw: "the-timer")

        assert poll_mod.poll(5.0, lambda: None, lambda d: None) == "the-timer"


class TestTheBlockingSitesUseIt:
    """Regression guard on the three timers this helper was built for.

    Measured, not assumed: of 38 `ui.timer` sites, 28 already took async
    callbacks and a further 7 of the remaining 10 do only cheap work (a
    datetime compare, a label update, an in-memory status read). Three read
    through a controller on the event loop:

      reversal_panel   pro_model_status() + get_risk_settings()  -- both DB
      news             get_events()  -- fetches ForexFactory when stale
      history/_heatmap get_app_config() + load_config()

    The heatmap one is deliberately NOT migrated: its timer body is a datetime
    compare, and it reaches the controller only inside a two-minute window once
    a day. Converting it would be churn with no stall removed. Recorded here so
    the omission is a decision rather than an oversight.
    """

    def _timer_callbacks(self, path):
        import ast
        import pathlib

        src = pathlib.Path(path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "timer" and len(node.args) > 1):
                cb = node.args[1]
                if isinstance(cb, ast.Name) and cb.id in fns:
                    out.append((cb.id, isinstance(fns[cb.id], ast.AsyncFunctionDef)))
        return out

    def test_the_reversal_learn_status_no_longer_polls_on_the_loop(self):
        names = [n for n, _ in self._timer_callbacks(
            "frontend/pages/reversal_panel/__init__.py")]

        assert "_refresh_learn_status" not in names

    def test_the_news_feed_no_longer_refreshes_on_the_loop(self):
        names = [n for n, _ in self._timer_callbacks("frontend/pages/news.py")]

        assert "_refresh" not in names

    def test_both_pages_use_the_shared_helper(self):
        import pathlib

        for page in ("frontend/pages/reversal_panel/__init__.py",
                     "frontend/pages/news.py"):
            src = pathlib.Path(page).read_text(encoding="utf-8")
            code = "\n".join(ln for ln in src.splitlines()
                             if not ln.strip().startswith("#"))
            assert "from frontend.components.poll import poll" in code, page
            assert "poll(" in code, page
