"""The volatility regime: one reference, a switch-gated ML tail, a backfill.

docs/todo/016 phase 2. What is pinned here:

  * the reference is a five-day median of the engine's own logged ATR, and
    it refuses to call fewer than MIN_SAMPLES readings "normal";
  * the feature is log(ratio), clamped, with 0.0 as the neutral;
  * with the switch OFF every tail is empty, so no vector changes width;
  * the backfill measures stored signals from the log, so turning the switch
    on reuses the whole training history instead of starting from zero.

Temp database. Nothing here reaches a broker.
"""
from __future__ import annotations

import math
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.reversal_engine import vol_regime as vr
from tests.conftest import remove_db_file

DAY = 86400.0


@pytest.fixture
def repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    re_repo.close_db()
    remove_db_file(path)


@pytest.fixture(autouse=True)
def _fresh_caches():
    vr._ref_cache.update(at=0.0, value=None)
    vr._switch_cache.update(at=0.0, value=False)
    yield
    vr._ref_cache.update(at=0.0, value=None)
    vr._switch_cache.update(at=0.0, value=False)


def _log(repo, ts, atr):
    repo.get_db().run(
        "INSERT INTO re_analysis_log (ts, atr, result) VALUES (?,?,?)", ts, atr, "x")


def _signal(repo, ref, created_at, atr):
    sid = repo.create_signal({"signal_ref": ref, "direction": "BUY",
                              "entry_low": 1.0, "entry_high": 1.0,
                              "stop_loss": 0.5, "atr": atr})
    repo.get_db().run("UPDATE re_signals SET created_at=? WHERE id=?", created_at, sid)
    return sid


def _ratio_of(repo, sid):
    return repo.get_db().get("SELECT atr_ratio FROM re_signals WHERE id=?", sid)["atr_ratio"]


# ── Pure ─────────────────────────────────────────────────────────────────────

class TestTheReference:
    def test_it_is_the_median(self):
        assert vr.reference_from([1.0] * 150 + [9.0] * 51) == 1.0

    def test_too_few_readings_is_no_reference(self):
        assert vr.reference_from([5.0] * (vr.MIN_SAMPLES - 1)) is None
        assert vr.reference_from([5.0] * vr.MIN_SAMPLES) == 5.0

    def test_zero_readings_do_not_count(self):
        assert vr.reference_from([5.0] * (vr.MIN_SAMPLES - 1) + [0.0] * 50) is None


class TestTheFeature:
    def test_normal_volatility_is_the_neutral(self):
        assert vr.feature(1.0) == 0.0 == vr.NEUTRAL

    def test_it_is_the_log_of_the_ratio(self):
        assert vr.feature(2.0) == pytest.approx(math.log(2.0), abs=1e-6)
        assert vr.feature(0.5) == pytest.approx(-math.log(2.0), abs=1e-6)

    def test_it_is_clamped(self):
        assert vr.feature(100.0) == vr.CLAMP
        assert vr.feature(0.001) == -vr.CLAMP

    @pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf"), "x"])
    def test_an_unusable_ratio_is_the_neutral(self, bad):
        assert vr.feature(bad) == vr.NEUTRAL

    def test_ratio_needs_both_sides(self):
        assert vr.ratio(10.0, 5.0) == 2.0
        assert vr.ratio(10.0, None) is None
        assert vr.ratio(0.0, 5.0) is None


class TestFitTail:
    BASE = 40

    def test_off_and_a_base_model_is_untouched(self):
        assert vr.fit_tail([1.0] * self.BASE, [], self.BASE, self.BASE) == [1.0] * self.BASE

    def test_on_and_a_model_not_yet_retrained_scores_the_base_vector(self):
        assert vr.fit_tail([1.0] * self.BASE, [0.7], self.BASE, self.BASE) == [1.0] * self.BASE

    def test_on_and_a_retrained_model_gets_the_tail(self):
        got = vr.fit_tail([1.0] * self.BASE, [0.7], self.BASE + 1, self.BASE)
        assert got == [1.0] * self.BASE + [0.7]

    def test_off_and_a_model_still_carrying_the_tail_gets_the_neutral(self):
        got = vr.fit_tail([1.0] * self.BASE, [], self.BASE + 1, self.BASE)
        assert got == [1.0] * self.BASE + [vr.NEUTRAL]

    def test_no_model_yet_takes_whatever_was_built(self):
        assert vr.fit_tail([1.0] * self.BASE, [0.7], None, self.BASE)[-1] == 0.7


# ── The switch ───────────────────────────────────────────────────────────────

class TestTheTailsAreEmptyWithTheSwitchOff:
    def test_live(self, monkeypatch):
        monkeypatch.setattr(vr, "feature_enabled", lambda now=None: False)
        monkeypatch.setattr(vr, "reference_atr_now", lambda now=None: 5.0)
        assert vr.live_tail({"atr": 10.0}) == []

    def test_live_with_it_on(self, monkeypatch):
        monkeypatch.setattr(vr, "feature_enabled", lambda now=None: True)
        monkeypatch.setattr(vr, "reference_atr_now", lambda now=None: 5.0)
        assert vr.live_tail({"atr": 10.0}) == [pytest.approx(math.log(2.0), abs=1e-6)]

    def test_stored(self):
        assert vr.row_tail({"atr_ratio": 2.0}, enabled=False) == []
        assert vr.row_tail({"atr_ratio": 2.0}, enabled=True) == [
            pytest.approx(math.log(2.0), abs=1e-6)]

    def test_an_unmeasured_row_is_neutral(self):
        assert vr.row_tail({"atr_ratio": None}, enabled=True) == [vr.NEUTRAL]


class TestTheSwitchIsRead:
    def test_off_by_default(self, monkeypatch):
        from backend.src.db import database as db
        monkeypatch.setattr(db, "get_risk_settings", lambda: {})
        assert vr.feature_enabled(now=1000.0) is False

    def test_on_is_on(self, monkeypatch):
        from backend.src.db import database as db
        monkeypatch.setattr(db, "get_risk_settings",
                            lambda: {"re_vol_regime_feature_enabled": 1})
        assert vr.feature_enabled(now=1000.0) is True

    def test_a_settings_failure_is_off(self, monkeypatch):
        from backend.src.db import database as db

        def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(db, "get_risk_settings", boom)
        assert vr.feature_enabled(now=1000.0) is False


# ── Against a database ───────────────────────────────────────────────────────

class TestTheReferenceIsReadFromTheLog:
    def test_five_days_before_and_nothing_after(self, repo):
        t = 100 * DAY
        for i in range(300):
            _log(repo, t - 4 * DAY + i * 60, 10.0)
        for i in range(300):
            _log(repo, t + 60 + i, 99.0)        # after: must not count
        for i in range(300):
            _log(repo, t - 6 * DAY + i, 99.0)   # before the window
        assert vr.reference_atr(t) == 10.0

    def test_latest_atr_ignores_a_stale_reading(self, repo):
        _log(repo, 1000.0, 7.0)
        assert vr.latest_atr(now=1000.0 + 60) == 7.0
        assert vr.latest_atr(now=1000.0 + vr.MAX_ATR_AGE_S + 1) is None


class TestTheBackfillReusesTheHistory:
    def test_a_stored_signal_gets_its_ratio(self, repo):
        t = 100 * DAY
        for i in range(300):
            _log(repo, t - 2 * DAY + i * 60, 10.0)
        sid = _signal(repo, "RE-1", t, atr=20.0)
        assert vr.backfill() == 1
        assert _ratio_of(repo, sid) == pytest.approx(2.0)

    def test_a_signal_the_log_cannot_measure_is_stored_as_neutral(self, repo):
        sid = _signal(repo, "RE-1", 100 * DAY, atr=20.0)
        vr.backfill()
        assert _ratio_of(repo, sid) == 0.0
        assert vr.feature(_ratio_of(repo, sid)) == vr.NEUTRAL

    def test_a_measured_row_is_not_read_again(self, repo):
        _signal(repo, "RE-1", 100 * DAY, atr=20.0)
        assert vr.backfill() == 1
        assert vr.backfill() == 0

    def test_it_works_through_the_history_in_batches(self, repo):
        for i in range(5):
            _signal(repo, f"RE-{i}", 100 * DAY + i, atr=20.0)
        assert vr.backfill(limit=2) == 2
        assert vr.backfill(limit=2) == 2
        assert vr.backfill(limit=2) == 1
        assert vr.backfill(limit=2) == 0

    def test_each_row_uses_its_own_window(self, repo):
        """Two signals a week apart, with different volatility before each:
        the batch read must slice per row, not use one median for all."""
        t1, t2 = 100 * DAY, 107 * DAY
        for i in range(300):
            _log(repo, t1 - DAY + i * 60, 10.0)
            _log(repo, t2 - DAY + i * 60, 40.0)
        s1 = _signal(repo, "RE-1", t1, atr=20.0)
        s2 = _signal(repo, "RE-2", t2, atr=20.0)
        vr.backfill()
        assert _ratio_of(repo, s1) == pytest.approx(2.0)
        assert _ratio_of(repo, s2) == pytest.approx(0.5)


class TestItIsOnTheTimer:
    def test_the_research_loop_runs_the_sweep(self):
        """Every minute, switch on or off: the history is measured before
        anyone turns the feature on, which is what makes the switch reuse it."""
        import asyncio
        from unittest import mock
        called = []

        async def _noop(engine):
            return None

        async def _sweep(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        loop = "backend.src.services.reversal_engine.research_loop"

        async def _go():
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch(f"{loop}._reversal_engine_research_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._reversal_engine_study_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._breakout_excursion_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._meta_label_refit_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._xasset_sweep_impl", side_effect=_noop), \
                 mock.patch(f"{loop}._vol_ratio_sweep_impl", side_effect=_sweep):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]
