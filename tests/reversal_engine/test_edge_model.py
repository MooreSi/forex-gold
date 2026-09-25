"""The edge model: a model that must prove itself before it may pass a trade.

docs/todo/reversal-engine/240. With `re_require_proven_edge` on, the
Reversal Engine places an order only when this model is proven -- the trades
it would have taken, predicted out of sample and walk-forward, made money on
the template's own exits with a cluster-robust t of 3 or more and in both
halves of history -- AND it predicts a non-negative R for this signal.

Every other model gate in this engine passes a signal when the model has no
opinion. This one refuses. Nothing here reaches a broker.
"""
import asyncio
import random

import pytest

from backend.src.services.reversal_engine import edge_model as em
from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES

T0 = 1_785_000_000.0


class TestTheProof:
    def _spread(self, n, mean, sd=1.0, seed=1, gap=7200.0):
        rng = random.Random(seed)
        return ([rng.gauss(mean, sd) for _ in range(n)],
                [T0 + gap * i for i in range(n)])

    def test_a_real_positive_mean_on_independent_trades_is_proven(self):
        r, ts = self._spread(600, 0.3)
        p = em.prove(r, ts)
        assert p["proven"] is True
        assert p["t"] >= 3

    def test_no_edge_is_not_proven(self):
        r, ts = self._spread(2000, 0.0, seed=4)
        assert em.prove(r, ts)["proven"] is False

    def test_too_few_accepted_trades_is_not_proven_however_good(self):
        r, ts = self._spread(150, 1.0)
        p = em.prove(r, ts)
        assert p["proven"] is False
        assert "150" in p["refusal"]

    def test_a_losing_half_is_not_proven(self):
        r1, ts1 = self._spread(400, 0.6, seed=2)
        r2, ts2 = self._spread(400, -0.05, seed=3)
        ts2 = [t + ts1[-1] + 7200 for t in ts2]
        p = em.prove(r1 + r2, ts1 + ts2)
        assert p["t"] >= 3
        assert p["proven"] is False
        assert "half" in p["refusal"]

    def test_trades_riding_one_move_count_as_one(self):
        # 400 winners, all inside two hours: one piece of evidence.
        r = [0.8 + 0.01 * (i % 7) for i in range(400)]
        ts = [T0 + 10 * i for i in range(400)]
        assert em.prove(r, ts)["proven"] is False


def _vec(signal_x, rng):
    """A full-width feature vector with `signal_x` in the first KEPT slot
    and noise elsewhere."""
    v = [rng.random() for _ in FEATURE_NAMES]
    v[em.KEPT[0]] = signal_x
    return v


def _rows(n, edge, seed=1, gap=5400.0):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        x = rng.random()
        r = (1.2 * (x - 0.5) + rng.gauss(0, 0.6)) if edge else rng.gauss(-0.1, 0.8)
        rows.append({"features": _vec(x, rng), "tpl_r": r,
                     "trigger_time": T0 + gap * i})
    return rows


class TestFitting:
    def test_a_learnable_edge_gets_proven(self):
        m = em.EdgeModel()
        st = m.fit(_rows(3000, edge=True))
        assert st.proven, st.refusal

    def test_noise_does_not(self):
        m = em.EdgeModel()
        st = m.fit(_rows(3000, edge=False, seed=9))
        assert not st.proven
        assert st.refusal

    def test_too_few_rows_is_refused_before_fitting(self):
        m = em.EdgeModel()
        st = m.fit(_rows(400, edge=True))
        assert not st.proven
        assert "400" in st.refusal

    def test_the_walk_forward_never_trains_on_the_future(self):
        seen = []

        class Spy:
            def fit(self, X, y):
                seen.append(len(X))
                self.m = sum(y) / len(y)
                return self

            def predict(self, X):
                return [self.m] * len(X)

        rows = _rows(2000, edge=True)
        m = em.EdgeModel(model_factory=Spy)
        m.fit(rows)
        folds = em.walk_forward(sorted(r["trigger_time"] for r in rows))
        for train_idx, test_idx in folds:
            test_start = rows[test_idx[0]]["trigger_time"]
            assert max(rows[i]["trigger_time"] for i in train_idx) \
                < test_start - em.EMBARGO_S
        # Five folds plus the final fit on everything.
        assert len(seen) == len(folds) + 1 and seen[-1] == 2000

    def test_the_dropped_features_are_not_read(self):
        dropped = [i for i, n in enumerate(FEATURE_NAMES) if n in em.DROPPED]
        assert dropped, "nothing is dropped"
        assert not set(dropped) & set(em.KEPT)
        assert "recent_win_rate" in em.DROPPED and "level_score" in em.DROPPED


class TestDeciding:
    def test_an_unfitted_model_refuses(self):
        ok, why, _ = em.EdgeModel().decide([0.5] * len(FEATURE_NAMES))
        assert ok is False and why

    def test_an_unproven_model_refuses_even_a_good_looking_signal(self):
        m = em.EdgeModel()
        m.fit(_rows(3000, edge=False, seed=9))
        ok, why, _ = m.decide(_vec(0.99, random.Random(1)))
        assert ok is False and "not proven" in why

    def test_a_proven_model_passes_what_it_predicts_positive(self):
        m = em.EdgeModel()
        m.fit(_rows(3000, edge=True))
        ok, _, pred = m.decide(_vec(0.95, random.Random(1)))
        assert ok is True and pred >= 0

    def test_a_proven_model_refuses_what_it_predicts_negative(self):
        m = em.EdgeModel()
        m.fit(_rows(3000, edge=True))
        ok, why, pred = m.decide(_vec(0.02, random.Random(1)))
        assert ok is False and pred < 0

    def test_no_features_is_a_refusal_not_a_pass(self):
        m = em.EdgeModel()
        m.fit(_rows(3000, edge=True))
        assert m.decide(None)[0] is False
        assert m.decide([1.0, 2.0])[0] is False

    def test_a_vector_from_a_newer_schema_is_refused_not_misread(self):
        # Longer than the schema: every index still resolves, so without the
        # length check it would be scored as if each slot meant what it
        # means today.
        m = em.EdgeModel()
        m.fit(_rows(3000, edge=True))
        longer = _vec(0.95, random.Random(1)) + [0.0, 0.0, 0.0]
        ok, why, _ = m.decide(longer)
        assert ok is False and "feature" in why


class TestTrainingRows:
    def test_rows_without_a_template_label_are_left_out(self):
        import json
        good = {"status": "closed", "tpl_r": 0.4, "trigger_time": T0,
                "ml_features_json": json.dumps([0.1] * len(FEATURE_NAMES))}
        unlabelled = dict(good, tpl_r=None)
        untriggered = dict(good, trigger_time=None)
        rows = em.rows_from_signals([good, unlabelled, untriggered])
        assert len(rows) == 1 and rows[0]["tpl_r"] == 0.4

    def test_short_historical_vectors_are_padded_like_training_pads_them(self):
        import json
        row = {"status": "closed", "tpl_r": 0.1, "trigger_time": T0,
               "ml_features_json": json.dumps([0.1] * (len(FEATURE_NAMES) - 5))}
        assert len(em.rows_from_signals([row])[0]["features"]) == len(FEATURE_NAMES)


class TestTheDailyRefit:
    def test_it_fits_once_per_day_and_again_after_a_restart(self, monkeypatch):
        calls = []
        monkeypatch.setattr(em, "_load_rows", lambda: calls.append(1) or [])
        monkeypatch.setattr(em, "_record", lambda st: None)
        em._last_fit_date = None
        asyncio.run(em.edge_model_refit_sweep(None, today="2026-09-24"))
        asyncio.run(em.edge_model_refit_sweep(None, today="2026-09-24"))
        asyncio.run(em.edge_model_refit_sweep(None, today="2026-09-25"))
        assert len(calls) == 2
        em._last_fit_date = None


class TestItIsOnTheTimer:
    def test_the_research_loop_runs_the_label_sweep_and_the_refit(self):
        from unittest import mock
        called = []

        async def _noop(engine):
            return None

        def _rec(name):
            async def _f(engine):
                called.append((name, engine))
            return _f

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
                 mock.patch(f"{loop}._tpl_label_sweep_impl", side_effect=_rec("label")), \
                 mock.patch(f"{loop}._edge_model_refit_sweep_impl", side_effect=_rec("edge")):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        # Labels first, so a day's refit sees the day's labels.
        assert called == [("label", engine), ("edge", engine)]


class TestItRefitsWhenTheLabelsGrow:
    """The label sweep backfills history one day a minute. A once-a-day fit
    taken at the start of that would judge 53 trades and hold that verdict
    until tomorrow (seen on the demo restart, 2026-09-24)."""

    def _run(self, monkeypatch, counts, today="2026-09-24"):
        fits = []
        monkeypatch.setattr(em, "_load_rows", lambda: fits.append(1) or [])
        monkeypatch.setattr(em, "_record", lambda st: None)
        seq = iter(counts)
        monkeypatch.setattr(em, "_count_labelled", lambda: next(seq))
        em._last_fit_date = None
        em._last_fit_n = 0
        for _ in counts:
            asyncio.run(em.edge_model_refit_sweep(None, today=today))
        em._last_fit_date = None
        em._last_fit_n = 0
        return len(fits)

    def test_a_backfill_that_grows_the_labels_refits_the_same_day(self, monkeypatch):
        assert self._run(monkeypatch, [53, 60, 400, 5600]) == 3

    def test_a_few_new_labels_do_not(self, monkeypatch):
        assert self._run(monkeypatch, [5600, 5620, 5650, 5700]) == 1
