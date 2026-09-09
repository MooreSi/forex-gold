"""The ML fit must not run on the asyncio event loop.

**Measured live 2026-09-09**, with the lines either side of it:

    18:32:55,573 [ProModel] fitted n=7769 (pos=1533 neg=6236) AUC=0.820 -> ok
    18:32:55,594 WARNING asyncio — Executing <Task pending name='Task-687'
                                    coro=<ReversalEngine._cycle_loop() ...
    18:32:55,596 [LoopMonitor] event loop stalled 4959ms (expected 250ms)

`fit()` trains five RandomForests of 300 trees (four cross-validation folds
plus the final model) over 7,769 rows. That takes about five seconds, and for
those five seconds **nothing else in the app runs** -- not the UI, not the EA
socket reader, not the monitor loop that manages open trades.

**Why that is a money problem, not a latency complaint.** The EA reconnects
when Python goes quiet for ten seconds, and a template-managed trade has no
Python fallback:

    15:19:52 [EA] trade=e44091e4 EA unhealthy -- template strategies have no
             Python fallback, leaving unmanaged until the EA reconnects
    15:19:55 [EABridge] no data from Python in 10s — reconnecting

A five-second freeze is half that budget.

**The behaviour change, made on the owner's explicit permission (2026-09-09,
"you can also fix money path issues").** A signal arriving before the first
successful fit now scores NEUTRAL instead of waiting for a model to train.
That is what already happens whenever scoring raises, and NEUTRAL is the
documented "not trustworthy yet" answer -- but it is a change to what the ML
gate sees on the first signals after a restart, so it is recorded plainly here
rather than buried.

**Mutants, and one that is genuinely equivalent.** Four were run: scoring
blocking on the fit again, `on_new_signal` fitting inline, the in-flight guard
removed, and the exception log removed -- all four killed, two of them only
after the tests below were strengthened (see their docstrings; the first
attempt at both was wrong about what it was protecting).

Widening the pool from `max_workers=1` to 4 **survives, and should.** The
in-flight guard means at most one fit is ever outstanding, so the worker count
cannot change behaviour. The 1 is documentation and defence in depth, not a
live constraint, and no test is contrived to pretend otherwise.
"""
from __future__ import annotations

import threading
import time

import pytest

from backend.src.services.reversal_engine import pro_model


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts with no model, and never touches the real corpus."""
    monkeypatch.setitem(pro_model._state, "model", None)
    monkeypatch.setitem(pro_model._state, "auc", None)
    yield
    pro_model.wait_for_fit(timeout=5.0)


@pytest.fixture
def slow_fit(monkeypatch):
    """A fit that takes long enough that a blocking caller cannot hide it."""
    seen = {"calls": 0, "threads": set(), "running": 0, "max_concurrent": 0}
    lock = threading.Lock()

    def _fit(force: bool = False):
        with lock:
            seen["calls"] += 1
            seen["threads"].add(threading.current_thread().name)
            seen["running"] += 1
            seen["max_concurrent"] = max(seen["max_concurrent"], seen["running"])
        time.sleep(0.4)
        with lock:
            seen["running"] -= 1
        return {}

    monkeypatch.setattr(pro_model, "fit", _fit)
    return seen


class TestScoringNeverWaitsForATrain:
    def test_pro_likeness_returns_immediately_when_no_model_is_ready(self, slow_fit):
        """It used to call fit() inline. On the real corpus that is 5 seconds
        of frozen event loop, per the log above."""
        t0 = time.monotonic()
        pro_model.pro_likeness("BUY", 50.0, 20.0, 3.0, 0.5)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.25, (
            f"scoring blocked for {elapsed:.2f}s -- it is waiting on the fit"
        )

    def test_and_it_answers_NEUTRAL_rather_than_guessing(self, slow_fit):
        assert pro_model.pro_likeness("BUY", 50.0, 20.0, 3.0, 0.5) == pro_model.NEUTRAL

    def test_but_it_DOES_start_the_fit(self, slow_fit):
        """Returning NEUTRAL forever would quietly disable the feature."""
        pro_model.pro_likeness("BUY", 50.0, 20.0, 3.0, 0.5)
        pro_model.wait_for_fit(timeout=5.0)

        assert slow_fit["calls"] == 1

    def test_the_fit_runs_off_the_calling_thread(self, slow_fit):
        pro_model.pro_likeness("BUY", 50.0, 20.0, 3.0, 0.5)
        pro_model.wait_for_fit(timeout=5.0)

        assert threading.current_thread().name not in slow_fit["threads"]


class TestTheRefitAfterEverySignal:
    def test_on_new_signal_does_not_block_either(self, slow_fit):
        """One incremental refit per captured signal is the design. Doing it
        inline is what put a five-second stall on the signal path."""
        t0 = time.monotonic()
        pro_model.on_new_signal()
        elapsed = time.monotonic() - t0

        assert elapsed < 0.25, f"on_new_signal blocked for {elapsed:.2f}s"

    def test_it_still_refits(self, slow_fit):
        pro_model.on_new_signal()
        pro_model.wait_for_fit(timeout=5.0)

        assert slow_fit["calls"] == 1


class TestFitsDoNotPileUp:
    def test_a_burst_of_signals_does_not_QUEUE_a_fit_each(self, slow_fit):
        """Signals arrive faster than a fit completes.

        The first version of this asserted `max_concurrent == 1` and **a
        mutant deleting the in-flight guard survived it** -- the pool has a
        single worker, so fits never overlap whether the guard is there or
        not. Concurrency was never the risk. The risk is the QUEUE: without
        the guard, eight signals enqueue eight full five-second trains that
        run back to back on unchanged data.

        So the assertion is on the number of fits actually started.
        """
        for _ in range(8):
            pro_model.on_new_signal()
        pro_model.wait_for_fit(timeout=10.0)

        assert slow_fit["calls"] <= 2, (
            f"{slow_fit['calls']} fits queued for 8 signals -- the in-flight "
            f"guard is not collapsing them"
        )
        assert slow_fit["max_concurrent"] == 1

    def test_a_fit_that_raises_does_not_wedge_the_next_one(self, monkeypatch):
        """The guard is a Future, and a Future that raised still reports
        `done()`, so the next fit is submitted either way.

        Recorded because the first version of this file claimed the try/except
        in `_fit_guarded` was what prevented a wedge, and a mutant removing it
        survived -- correctly. It is not load-bearing for wedging. What it
        actually buys is a log line: nothing on the trading path ever calls
        `.result()`, so without it a failing fit is swallowed by the Future in
        total silence. That is asserted separately below.
        """
        calls = {"n": 0}

        def _boom(force: bool = False):
            calls["n"] += 1
            raise RuntimeError("sklearn exploded")

        monkeypatch.setattr(pro_model, "fit", _boom)
        pro_model.on_new_signal()
        pro_model.wait_for_fit(timeout=5.0)
        pro_model.on_new_signal()
        pro_model.wait_for_fit(timeout=5.0)

        assert calls["n"] == 2, "the second fit never ran -- the guard is stuck"

    def test_a_failing_fit_is_logged_rather_than_swallowed_silently(
            self, monkeypatch, caplog):
        """Nothing on the trading path calls `.result()`, so an unguarded
        exception inside the executor disappears without a trace. This is what
        `_fit_guarded`'s try/except is actually for."""
        def _boom(force: bool = False):
            raise RuntimeError("sklearn exploded")

        monkeypatch.setattr(pro_model, "fit", _boom)
        with caplog.at_level("DEBUG", logger=pro_model.log.name):
            pro_model.on_new_signal()
            pro_model.wait_for_fit(timeout=5.0)

        assert any("background fit failed" in r.getMessage()
                   for r in caplog.records), "the failure left no log line"
