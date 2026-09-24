"""The Reversal Engine retrains without stopping everything else.

Measured on the owner's Mac, 2026-09-23: `ReversalEngine._outcome_loop` held
the event loop for ~0.42 s eleven times in a day, and the nightly research run
at 22:00 did the same. Profiled on a copy of the live data, 2026-09-24:
`_get_training_data` 103 ms, `_retrain` 354 ms, `_save_all` 8 ms. Every fifth
closed signal ran the whole retrain inline, and nothing else in the process --
position monitor, EA link, Telegram -- ran until it finished.

**What must not change is the model the live gate reads.** The gate fails
OPEN (docs/system/domains/engines/README.md): `predict()` returning None lets
every signal through unfiltered. The old `_retrain` assigned a new, UNFITTED
model to `_model_batch` and only then fitted it -- harmless while nothing else
could run in between, a gap in the gate once anything could. So the fit now
happens on a worker thread into a local model, and the fitted model is swapped
in on the loop in one assignment. At every instant `_model_batch` is either
the old fitted model or the new fitted one.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from backend.src.services.reversal_engine import ml_engine as m


def _row(width, net, seed):
    feats = [round(0.1 + (seed % 7) * 0.05 + i * 0.001, 4) for i in range(width)]
    return {"ml_features_json": json.dumps(feats),
            "outcome": "win" if net > 0 else "loss",
            "sl_dist": 7.0, "net_pnl_dollars": net}


def _history(n=40):
    width = len(m.FEATURE_NAMES)
    return [_row(width, 30.0 if i % 3 else -70.0, i) for i in range(n)]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    monkeypatch.setattr(m, "_data_dir", tmp_path)
    monkeypatch.setattr(m, "_model_batch", None)
    monkeypatch.setattr(m, "_model_online", None)
    monkeypatch.setattr(m, "_labeled_count", 0)
    monkeypatch.setattr(m, "_train_history", [])
    monkeypatch.setattr(re_db, "get_ml_training_data", lambda: _history())
    return tmp_path


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestTheFitRunsOffTheLoop:
    def test_the_async_retrain_fits_on_a_worker_thread(self, isolated, monkeypatch):
        seen = {}
        real = m._fit_batch

        def _spy(X, y):
            seen["on_loop"] = _on_the_event_loop()
            return real(X, y)
        monkeypatch.setattr(m, "_fit_batch", _spy)

        asyncio.run(m.retrain_async())

        assert seen["on_loop"] is False
        assert m._model_batch is not None

    def test_the_training_data_is_read_off_the_loop_too(self, isolated, monkeypatch):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        seen = {}

        def _rows():
            seen["on_loop"] = _on_the_event_loop()
            return _history()
        monkeypatch.setattr(re_db, "get_ml_training_data", _rows)

        asyncio.run(m.retrain_async())

        assert seen["on_loop"] is False

    def test_the_result_is_the_same_model_the_inline_retrain_builds(self, isolated):
        asyncio.run(m.retrain_async())
        threaded = (m._labeled_count, m._model_batch.n_features_in_)

        m._model_batch = None
        m._retrain()

        assert (m._labeled_count, m._model_batch.n_features_in_) == threaded


class TestTheGateNeverSeesAnUnfittedModel:
    def test_the_old_model_stays_in_place_until_the_new_one_is_fitted(
        self, isolated, monkeypatch,
    ):
        """The gate fails open. A model swapped in before it is fitted makes
        `predict()` raise, which it swallows -- and with no online model that
        is a None, and a None executes every signal unfiltered."""
        m._retrain()
        old = m._model_batch
        during: dict = {}
        fitting = threading.Event()
        release = threading.Event()
        real = m._fit_batch

        def _slow(X, y):
            fitting.set()
            release.wait(5)
            return real(X, y)
        monkeypatch.setattr(m, "_fit_batch", _slow)

        async def _scenario():
            task = asyncio.create_task(m.retrain_async())
            await asyncio.to_thread(fitting.wait, 5)
            during["model"] = m._model_batch
            during["prediction"] = m.predict([0.2] * len(m.FEATURE_NAMES))
            release.set()
            await task

        asyncio.run(_scenario())

        assert during["model"] is old
        assert isinstance(during["prediction"], float)
        assert m._model_batch is not old

    def test_the_inline_retrain_also_swaps_only_a_fitted_model(self, isolated, monkeypatch):
        """The sync path builds into a local too, so a caller on another
        thread never observes the half-built one."""
        observed = []
        real = m._fit_batch

        def _watch(X, y):
            observed.append(m._model_batch)
            return real(X, y)
        monkeypatch.setattr(m, "_fit_batch", _watch)

        m._retrain()

        assert observed == [None]
        assert m._model_batch is not None


class TestAClosedSignalAsksForTheRetrain:
    def test_inside_a_running_loop_the_retrain_is_scheduled_not_run_inline(
        self, isolated, monkeypatch,
    ):
        calls = []
        monkeypatch.setattr(m, "_retrain", lambda: calls.append("inline"))

        async def _fake_async():
            calls.append("async")
        monkeypatch.setattr(m, "retrain_async", _fake_async)

        async def _scenario():
            m._request_retrain()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_scenario())

        assert calls == ["async"]

    def test_with_no_loop_it_retrains_inline_as_before(self, isolated, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "_retrain", lambda: calls.append("inline"))

        m._request_retrain()

        assert calls == ["inline"]

    def test_two_requests_in_flight_run_one_retrain_then_one_more(
        self, isolated, monkeypatch,
    ):
        """A retrain requested while one is running is not dropped (it would
        leave the model a batch behind) and not run concurrently (two fits
        racing to install)."""
        started = []

        async def _scenario():
            release = asyncio.Event()

            async def _slow():
                started.append(len(started))
                await release.wait()
            monkeypatch.setattr(m, "retrain_async", _slow)

            m._request_retrain()
            await asyncio.sleep(0)
            m._request_retrain()
            m._request_retrain()
            await asyncio.sleep(0)
            assert started == [0]
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)

        asyncio.run(_scenario())

        assert started == [0, 1]

    def test_record_outcome_uses_it(self):
        import inspect
        src = inspect.getsource(m.record_outcome)
        assert "_request_retrain()" in src
        assert "_retrain()" not in src.replace("_request_retrain()", "")


class TestTheNightlyResearchRetrainsOffTheLoop:
    def test_it_awaits_the_async_retrain(self):
        import inspect
        from backend.src.services.reversal_engine import telegram_research
        src = inspect.getsource(telegram_research.run_nightly_research)
        assert "await re_ml.retrain_async()" in src
        assert "retrain_now()" not in src
