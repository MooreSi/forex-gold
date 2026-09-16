"""Every dashboard timer says which one it was when it stalls the loop.

`docs/todo/bugs/030`. With the panel re-render stalls gone,
`Timer._invoke_callback()` is 84-88% of what is left, and Python cannot tell
you which timer it was. Every one of the frontend's `ui.timer` call sites
produces the identical warning:

    coro=<Timer._invoke_callback() done, defined at .../nicegui/timer.py:107>
    created at backend/src/utils/background_tasks.py:40

The user callback is awaited INSIDE that task, so the coroutine name is
NiceGUI's. Reading the source location out of the warning is what attributed
the panel re-renders to two exact lines; here it names the framework and
stops. This module puts the name back, so the same aggregation that cracked
the panels can be run one layer down.

Attribution first. This file measures nothing and fixes nothing -- it makes
the next measurement able to say a name.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import time

import pytest

from frontend.components import timer_probe

REPO = pathlib.Path(__file__).resolve().parents[2]


def _slow():
    time.sleep(timer_probe.SLOW_TIMER_S + 0.05)


def _fast():
    return "done"


async def _slow_async():
    await asyncio.sleep(timer_probe.SLOW_TIMER_S + 0.05)


async def _fast_async():
    return "done"


class TestItNamesWhatWasSlow:
    def test_a_slow_callback_is_logged_with_its_name(self, caplog):
        with caplog.at_level(logging.WARNING):
            timer_probe.timed(_slow)()
        assert "_slow" in caplog.text

    def test_a_fast_callback_is_not_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            timer_probe.timed(_fast)()
        assert caplog.text == ""

    def test_a_slow_async_callback_is_logged_with_its_name(self, caplog):
        with caplog.at_level(logging.WARNING):
            asyncio.run(timer_probe.timed(_slow_async)())
        assert "_slow_async" in caplog.text

    def test_a_fast_async_callback_is_not_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            asyncio.run(timer_probe.timed(_fast_async)())
        assert caplog.text == ""

    def test_the_log_line_carries_the_duration(self, caplog):
        with caplog.at_level(logging.WARNING):
            timer_probe.timed(_slow)()
        assert "0.4" in caplog.text or "0.5" in caplog.text

    def test_an_explicit_label_wins_over_the_callbacks_name(self, caplog):
        """A lambda or a closure named `_tick` in nine files needs a name the
        aggregation can tell apart."""
        with caplog.at_level(logging.WARNING):
            timer_probe.timed(_slow, label="header/github-check")()
        assert "header/github-check" in caplog.text

    def test_an_anonymous_callback_still_gets_a_usable_name(self, caplog):
        """`<lambda>` in the log is no better than `Timer._invoke_callback`.
        The call site is what makes it findable."""
        with caplog.at_level(logging.WARNING):
            timer_probe.timed(lambda: _slow())()
        assert "test_timer_probe.py" in caplog.text


class TestItChangesNothingAboutTheCallback:
    """A probe that alters behaviour is worse than no probe. It sits on
    thirty-odd live dashboard timers."""

    def test_the_return_value_is_passed_through(self):
        assert timer_probe.timed(_fast)() == "done"

    def test_the_async_return_value_is_passed_through(self):
        assert asyncio.run(timer_probe.timed(_fast_async)()) == "done"

    def test_an_exception_propagates_unchanged(self):
        def _boom():
            raise ValueError("the panel is gone")

        with pytest.raises(ValueError, match="the panel is gone"):
            timer_probe.timed(_boom)()

    def test_a_raising_callback_is_still_timed(self, caplog):
        """Otherwise the slowest thing a timer does -- fail slowly -- is the
        one case that never gets attributed."""
        def _slow_boom():
            time.sleep(timer_probe.SLOW_TIMER_S + 0.05)
            raise ValueError("boom")

        with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
            timer_probe.timed(_slow_boom)()
        assert "_slow_boom" in caplog.text

    def test_a_raising_async_callback_is_still_timed(self, caplog):
        """The sync case below had this covered and the async one did not --
        found by a mutant that swapped the async `finally` for an `except`
        and survived."""
        async def _slow_boom_async():
            await asyncio.sleep(timer_probe.SLOW_TIMER_S + 0.05)
            raise ValueError("boom")

        with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
            asyncio.run(timer_probe.timed(_slow_boom_async)())
        assert "_slow_boom_async" in caplog.text

    def test_arguments_are_passed_through(self):
        assert timer_probe.timed(lambda a, b=0: a + b)(1, b=2) == 3

    def test_a_sync_callback_stays_sync(self):
        """NiceGUI treats a coroutine function differently from a plain one.
        Wrapping a sync callback in an async one would change when it runs."""
        assert not asyncio.iscoroutinefunction(timer_probe.timed(_fast))

    def test_an_async_callback_stays_async(self):
        assert asyncio.iscoroutinefunction(timer_probe.timed(_fast_async))


class TestTheThresholdMatchesTheOneDoingTheMeasuring:
    def test_it_agrees_with_the_loop_monitors_threshold(self):
        """The whole point is that a `[SlowTimer]` line and an
        `Executing <Task ...> took N seconds` line describe the same event.
        A frontend module may not import `backend.src.utils` -- the
        import contract allows only `backend.src.controllers` -- so the
        constant is duplicated, and this is what stops the copies drifting.
        """
        from backend.src.utils import loop_monitor
        assert timer_probe.SLOW_TIMER_S == loop_monitor._WARN_THRESHOLD_S


class TestEveryTimerGoesThroughIt:
    """The mechanism being right is worth nothing if the next timer added
    skips it. Same failure the previous attempt at 030 died of:
    `change_signature` worked, and the only panel calling it was deleted.
    """

    def _raw_timer_sites(self):
        found = []
        for path in sorted((REPO / "frontend").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED_RAW:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "timer"
                        and isinstance(f.value, ast.Name)
                        and f.value.id in _NICEGUI_ALIASES):
                    found.append(f"{rel}:{node.lineno}")
        return found

    def test_no_page_calls_ui_timer_directly(self):
        assert self._raw_timer_sites() == []

    def test_the_scanner_finds_a_planted_one(self, tmp_path, monkeypatch):
        """Negative control. A scanner that cannot fail is not a check.

        The offender is planted in a real file under `frontend/`, and the
        whole walk is re-run, so this exercises the same code path as the
        assertion above rather than a re-implementation of it.
        """
        planted = REPO / "frontend" / "components" / "_planted_for_test.py"
        planted.write_text("from nicegui import ui\n\n\n"
                           "def _x():\n    ui.timer(1.0, print)\n",
                           encoding="utf-8")
        try:
            sites = self._raw_timer_sites()
        finally:
            planted.unlink()
        assert sites == ["frontend/components/_planted_for_test.py:5"]

    def test_the_planted_file_is_gone_again(self):
        assert not (REPO / "frontend" / "components"
                    / "_planted_for_test.py").exists()


# `frontend/pages/settings/_diagnostics.py` imported nicegui as `_ui`, so a
# scanner keyed on the literal name `ui` walked straight past it. Aliases are
# how a "zero violations" check quietly becomes a check of nothing.
_NICEGUI_ALIASES = {"ui", "_ui", "nicegui"}

# The only files allowed to call `ui.timer` directly: the two helpers that
# wrap it. Shrink-only -- it may lose entries and must never gain one.
ALLOWED_RAW = {
    "frontend/components/timer_probe.py",
    "frontend/components/poll.py",
}


class TestThePollHelperIsNamedToo:
    """`poll()` builds every tick from the same factory, so all four polls
    would otherwise reach the log as `make_tick.<locals>._tick` -- the same
    indistinguishable name this module exists to replace, one level in."""

    def test_the_label_names_the_read_rather_than_the_shared_factory(self):
        from frontend.components import poll as poll_mod

        def news_events():
            return []

        label = poll_mod._poll_label(news_events)
        assert "news_events" in label
        # The name it must NOT be. Asserted on the factory rather than on
        # "_tick", which this test's own method name would have supplied.
        assert "make_tick" not in label

    def test_poll_actually_hands_that_label_to_the_probe(self, monkeypatch):
        """The test above only proves the helper computes a good name. A
        mutant that stopped `poll()` PASSING it survived, which is the whole
        difference between a mechanism and a wired mechanism."""
        from frontend.components import poll as poll_mod

        seen = {}

        def _fake_timed(callback, *, label=None):
            seen["label"] = label
            return callback

        monkeypatch.setattr(poll_mod, "_timed", _fake_timed)
        monkeypatch.setattr(poll_mod.ui, "timer",
                            lambda *a, **k: "timer-object")

        def news_events():
            return []

        assert poll_mod.poll(5.0, news_events, lambda _d: None) == "timer-object"
        assert "news_events" in (seen["label"] or "")


class TestTheStallWarningCarriesTheName:
    """The correction of 2026-09-16, made the same afternoon it shipped.

    The first version of this module timed the callback with a wall clock and
    the docstring claimed a `[SlowTimer]` line and an asyncio
    `Executing <Task ...> took N seconds` line "describe the same event".
    They do not. asyncio's `slow_callback_duration` hook fires inside
    `Handle._run()` and measures ONE SYNCHRONOUS SLICE, which is real
    loop-blocking time. A wall clock around an `async def` includes every
    `await`, during which the loop is free.

    Measured: twelve `[SlowTimer]` lines against **zero**
    `Timer._invoke_callback` stalls in the same window, with all seven
    flagged callbacks `async def`. The instrument was reporting HTTP round
    trips as if they were stalls.

    The fix inverts it. asyncio keeps the measuring, because it measures the
    right thing. This module supplies only the name, by renaming the task
    NiceGUI already creates per invocation (`timer.py:95`,
    `asyncio.create_task(self._invoke_callback())`), so the name travels in
    the warning that was already correct.
    """

    def test_an_async_callback_renames_its_task(self):
        seen = {}

        async def _cb():
            seen["name"] = asyncio.current_task().get_name()

        asyncio.run(timer_probe.timed(_cb, label="header/refresh")())
        assert seen["name"] == "header/refresh"

    def test_a_sync_callback_renames_its_task_too(self):
        """NiceGUI runs sync callbacks inside the same per-invocation task,
        so these are attributable by the same route."""
        seen = {}

        def _cb():
            seen["name"] = asyncio.current_task().get_name()

        async def _go():
            timer_probe.timed(_cb, label="trading/account")()

        asyncio.run(_go())
        assert seen["name"] == "trading/account"

    def test_the_name_is_set_before_the_body_runs(self):
        """A stall happens INSIDE the body. A name applied afterwards would
        miss every event it exists to attribute."""
        seen = {}

        def _cb():
            seen["at_entry"] = asyncio.current_task().get_name()

        async def _go():
            timer_probe.timed(_cb, label="set-first")()

        asyncio.run(_go())
        assert seen["at_entry"] == "set-first"

    def test_outside_a_task_it_does_not_raise(self):
        """`timed` is a plain wrapper and nothing stops it being called from
        synchronous code with no running loop."""
        assert timer_probe.timed(_fast, label="no-task")() == "done"

    def test_the_real_asyncio_warning_carries_the_label(self, caplog):
        """The whole point, end to end, through asyncio's own hook rather
        than a re-implementation of it: block the loop synchronously inside
        a renamed task with debug mode on, and read the stdlib warning.
        """
        def _block():
            time.sleep(timer_probe.SLOW_TIMER_S + 0.1)

        async def _go():
            loop = asyncio.get_running_loop()
            loop.set_debug(True)
            loop.slow_callback_duration = timer_probe.SLOW_TIMER_S
            await asyncio.create_task(_as_coro(timer_probe.timed(
                _block, label="chart/refresh_fast")))

        with caplog.at_level(logging.WARNING, logger="asyncio"):
            asyncio.run(_go())

        asyncio_lines = [r.getMessage() for r in caplog.records
                         if r.name == "asyncio" and "took" in r.getMessage()]
        assert asyncio_lines, "asyncio did not report the block at all"
        assert any("chart/refresh_fast" in m for m in asyncio_lines)


async def _as_coro(fn):
    return fn()


class TestItDoesNotClaimWallClockIsLoopTime:
    def test_the_latency_line_says_the_number_includes_awaits(self, caplog):
        """The wall-clock line is kept -- a slow refresh is worth knowing --
        but it must never again read as a stall. The wording is asserted so
        the claim cannot quietly come back."""
        with caplog.at_level(logging.WARNING):
            asyncio.run(timer_probe.timed(_slow_async)())
        assert "await" in caplog.text.lower()

    def test_the_module_does_not_claim_the_two_lines_are_one_event(self):
        import pathlib as _p
        import re as _re
        src = (_p.Path(timer_probe.__file__)).read_text(encoding="utf-8")
        # Whitespace-normalised. The first version of this assertion looked
        # for the literal phrase and passed while the claim was still in the
        # file, because the docstring wrapped it across a line break.
        flat = _re.sub(r"\s+", " ", src)
        assert "describe the same event" not in flat
