"""docs/todo/017 items 1 and 3, as the Breakout model sees them.

3. `bo_ml_realised_r_label`: the label is realised R (net dollars over the
   signal's own risk), not rr_tp1 / -1 / 0; a model fitted under the old label
   is not used under the new one; and the model has no opinion until a
   time-ordered holdout shows it ranks trades (AUC >= 0.55 on >= 60 rows).
   Measured 2026-10-14 on 157 closed rows: rr_tp1 is 1.0 on every one, wins
   realise +1.02R and losses -0.84R.
1. `bo_ml_time_decay_enabled`: time-decay x uniqueness weights replace the
   40-trade half-life.

Off, both leave the fit and the label exactly as before. Fakes only.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from backend.src.services.breakout_signal import ml_engine as ml
from backend.src.services.breakout_signal import ml_handover as ho
from backend.src.services.breakout_signal import ml_labels as lb
from backend.src.services.market import sample_weights as sw

N = ml.N_FEATURES
HOUR = 3600.0


def _row(i, *, outcome, net, sl=10.0, lot=0.1, rr=1.0, signal=None):
    feats = [0.1 * ((i * 7) % 11)] * N if signal is None else signal
    t0 = 1_760_000_000.0 + i * HOUR
    return {"id": i + 1, "ml_features_json": json.dumps(feats),
            "outcome": outcome, "rr_tp1": rr, "sl_dist": sl, "lot_size": lot,
            "net_pnl_dollars": net, "pnl_dollars": net,
            "live_exec_status": None, "created_at": t0,
            "trigger_time": t0 + 60.0, "close_time": t0 + 1800.0}


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "_model", None)
    monkeypatch.setattr(ml, "_model_online", None)
    monkeypatch.setattr(ml, "_data_dir", tmp_path)
    monkeypatch.setattr(ml, "_labeled_count", 0)
    monkeypatch.setattr(ml, "_new_since_retrain", 0)
    monkeypatch.setattr(ml, "_train_history", [])
    monkeypatch.setattr(ml, "_label_mode", lb.LEGACY)
    monkeypatch.setattr(ml, "_gate_armed", True)
    monkeypatch.setattr(ho, "legacy_width", None)
    state = {"decay": False, "realised": False}
    monkeypatch.setattr(lb, "switches", lambda: (state["decay"], state["realised"]))
    return state


def _capture_fit(monkeypatch):
    """Record the labels and weights the LightGBM fit receives."""
    import lightgbm as lgb
    seen = {}
    real = lgb.Dataset

    def _ds(X, label=None, weight=None, **kw):
        seen["y"] = list(label)
        seen["w"] = list(weight)
        return real(X, label=label, weight=weight, **kw)
    monkeypatch.setattr(lgb, "Dataset", _ds)
    return seen


def _history(n=40):
    rows = []
    for i in range(n):
        win = i % 3 != 0
        rows.append(_row(i, outcome="win" if win else "loss",
                         net=102.0 if win else -84.0))
    return rows


class TestTheLabel:
    def test_realised_r_is_net_over_the_signals_own_risk(self):
        assert lb.realised_r(_row(0, outcome="win", net=102.0)) == pytest.approx(1.02)

    def test_falls_back_to_gross_when_net_is_missing(self):
        r = _row(0, outcome="loss", net=None)
        r["pnl_dollars"] = -84.0
        assert lb.realised_r(r) == pytest.approx(-0.84)

    def test_the_old_label_reads_it_as_one(self):
        assert lb.legacy_label("win", 1.0) == 1.0
        assert lb.legacy_label("loss", 1.0) == -1.0
        assert lb.legacy_label("be", 1.0) == 0.0

    @pytest.mark.parametrize("bad", [{"sl_dist": 0.0}, {"sl_dist": None},
                                     {"net_pnl_dollars": None, "pnl_dollars": None},
                                     {"live_exec_status": "executed"}])
    def test_unusable_rows_have_no_realised_label(self, bad):
        r = _row(0, outcome="win", net=102.0)
        r.update(bad)
        assert lb.realised_r(r) is None

    def test_clamped(self):
        assert lb.realised_r(_row(0, outcome="win", net=1e6)) == lb.R_CLAMP


class TestOffIsToday:
    def test_the_fit_sees_the_old_label_and_the_old_weights(self, monkeypatch):
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        rows = _history()
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: rows)
        seen = _capture_fit(monkeypatch)
        ml._retrain()
        n = len(rows)
        assert seen["y"] == [1.0 if r["outcome"] == "win" else -1.0 for r in rows]
        assert seen["w"] == pytest.approx(
            [np.exp(np.log(0.5) / ml.DECAY_HALFLIFE * (n - 1 - i)) for i in range(n)])

    def test_predict_is_unaffected_by_the_arming_rule(self, monkeypatch):
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: _history(20))
        ml._retrain()
        assert ml.predict([0.3] * N) is not None


class TestRealisedOn:
    def test_the_fit_sees_realised_r(self, clean, monkeypatch):
        clean["realised"] = True
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        rows = _history()
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: rows)
        seen = _capture_fit(monkeypatch)
        ml._retrain()
        assert seen["y"][:3] == pytest.approx([-0.84, 1.02, 1.02])

    def test_too_few_rows_to_judge_means_no_opinion(self, clean, monkeypatch):
        clean["realised"] = True
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: _history(40))
        ml._retrain()
        assert ml._gate_armed is False
        assert ml.predict([0.3] * N) is None

    def test_a_model_that_ranks_its_holdout_is_armed(self, clean, monkeypatch):
        clean["realised"] = True
        rows = []
        for i in range(120):
            win = i % 2 == 0
            sig = [1.0 if win else 0.0] + [0.1 * (i % 5)] * (N - 1)
            rows.append(_row(i, outcome="win" if win else "loss",
                             net=102.0 if win else -84.0, signal=sig))
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: rows)
        ml._retrain()
        assert ml._gate_armed is True
        assert ml.predict([1.0] + [0.0] * (N - 1)) > ml.predict([0.0] * N)

    def test_models_of_noise_are_rarely_armed(self, clean, monkeypatch):
        """Over 40 noise histories, at most a 5% test's share arms (allowing
        for sampling: 5 of 40). Measured 2026-10-14 on histories like these: AUC
        >= 0.55 alone armed 6 of 40, the rule with its significance bar 0."""
        clean["realised"] = True
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        armed = 0
        for seed in range(40):
            rng = np.random.default_rng(seed)
            rows = []
            for i in range(120):
                win = bool(rng.integers(0, 2))
                rows.append(_row(i, outcome="win" if win else "loss",
                                 net=102.0 if win else -84.0,
                                 signal=list(rng.random(N))))
            monkeypatch.setattr(bdb, "get_ml_training_data", lambda r=rows: r)
            ml._model_online = None
            ml._retrain()
            armed += ml._gate_armed
        assert armed <= 5

    def test_the_significance_bar(self):
        assert lb.armed((0.60, 1.0)) is False     # high but not significant
        assert lb.armed((0.54, 3.0)) is False     # significant but below 0.55
        assert lb.armed((0.60, 2.0)) is True
        assert lb.armed(None) is False
        assert lb.auc_z(0.5, 15, 15) == 0.0

    def test_a_model_fitted_under_the_old_label_is_not_used(self, clean, monkeypatch):
        """Flip the switch with an old-label model loaded: the next prediction
        refits first and the old online learner is dropped."""
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: _history(40))
        ml._retrain()
        ml._online_update([0.3] * N, 1.0)
        old_batch = ml._model
        assert ml._model_online is not None and ml._label_mode == lb.LEGACY

        clean["realised"] = True
        ml.predict([0.3] * N)
        assert ml._label_mode == lb.REALISED
        assert ml._model is not old_batch
        assert ml._model_online is None

    def test_a_loss_is_not_double_weighted_online(self):
        """The 2x existed to make up for a label that priced every loss at
        -1R; realised R carries the size itself (Reversal dropped it at v5)."""
        assert lb.online_weight(-0.84, lb.REALISED) == 1.0
        assert lb.online_weight(-1.0, lb.LEGACY) == 2.0
        assert lb.online_weight(1.0, lb.LEGACY) == 1.0


class TestTimeDecayOn:
    def test_the_fit_gets_the_shared_weights(self, clean, monkeypatch):
        clean["decay"] = True
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        rows = _history()
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: rows)
        seen = _capture_fit(monkeypatch)
        ml._retrain()
        spans = [(r["trigger_time"], r["close_time"]) for r in rows]
        assert seen["w"] == pytest.approx(sw.training_weights(
            spans, lb.DECAY_HALF_LIFE_DAYS, lb.DECAY_FLOOR))
