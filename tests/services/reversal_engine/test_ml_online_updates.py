"""Online failure/recovery uses private SQLite and model files; no broker calls."""
import json
import logging

import joblib
import numpy as np
import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine import reversal_engine_repo as repo


class Estimator:
    n_features_in_ = len(ml.FEATURE_NAMES)

    def __init__(self, mode="ok"):
        self.mode = mode
        self.coef_ = np.array([0.2])
        self.intercept_ = np.array([0.0])

    def partial_fit(self, X, y):
        self.coef_[0] = 0.3
        if self.mode == "raise":
            raise ValueError("injected fit failure")
        if self.mode == "nan":
            self.coef_[0] = np.nan
        if self.mode == "inf":
            self.intercept_[0] = np.inf
        return self

    def predict(self, X):
        return [float(self.coef_[0])]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    repo.init(str(tmp_path / "reversal.db"))
    signal_id = repo.create_signal({
        "created_at": 100, "signal_ref": "RE-online-test", "direction": "BUY",
        "status": "closed", "outcome": "win", "sl_dist": 5,
        "net_pnl_dollars": 10,
        "ml_features_json": json.dumps([0.1] * len(ml.FEATURE_NAMES)),
    })
    monkeypatch.setattr(ml, "_data_dir", tmp_path)
    monkeypatch.setattr(ml, "_model_batch", None)
    monkeypatch.setattr(ml, "_model_online", Estimator())
    monkeypatch.setattr(ml, "_labeled_count", 18)
    monkeypatch.setattr(ml, "_train_history", [])
    monkeypatch.setattr(ml, "_ref_level_stats", {})
    monkeypatch.setattr(ml, "_online_update_health", {
        "status": "unknown", "successful_updates": 0, "failed_updates": 0,
        "last_signal_id": None, "last_error": None,
    }, raising=False)
    yield signal_id, tmp_path
    repo.close_db()


def test_mutating_failed_update_keeps_prior_model_and_logs_failure(isolated, caplog):
    signal_id, path = isolated
    prior = Estimator("raise")
    ml._model_online = prior

    with caplog.at_level(logging.ERROR, logger=ml.__name__):
        ml.record_outcome(signal_id, "win")

    assert ml._model_online is prior
    assert prior.coef_[0] == 0.2
    assert f"signal={signal_id}" in caplog.text
    assert "injected fit failure" in caplog.text
    assert caplog.records[-1].exc_info is not None
    health = ml.summary()["online_update_health"]
    assert health["status"] == "error"
    assert health["successful_updates"] == 0
    assert health["failed_updates"] == 1
    assert health["last_signal_id"] == signal_id
    assert "ValueError" in health["last_error"]
    assert ml._labeled_count == 19
    assert joblib.load(path / "re_ml_online.pkl").coef_[0] == 0.2


@pytest.mark.parametrize("mode", ["nan", "inf"])
def test_nonfinite_fitted_parameters_are_not_published(isolated, mode):
    signal_id, _ = isolated
    prior = Estimator(mode)
    ml._model_online = prior

    ml.record_outcome(signal_id, "win")

    assert ml._model_online is prior
    assert prior.coef_[0] == 0.2
    assert prior.intercept_[0] == 0.0
    assert ml.summary()["online_update_health"]["failed_updates"] == 1


def test_healthy_update_is_installed_and_persisted(isolated):
    signal_id, path = isolated
    prior = ml._model_online

    ml.record_outcome(signal_id, "win")

    assert ml._model_online is not prior
    assert prior.coef_[0] == 0.2
    assert ml._model_online.coef_[0] == 0.3
    health = ml.summary()["online_update_health"]
    assert health["status"] == "ok"
    assert health["successful_updates"] == 1
    assert health["failed_updates"] == 0
    assert health["last_error"] is None
    assert joblib.load(path / "re_ml_meta.pkl")["online_update_health"] == health
    assert joblib.load(path / "re_ml_online.pkl").coef_[0] == 0.3


def test_success_after_failure_clears_error_but_keeps_failure_count(isolated):
    signal_id, _ = isolated
    ml._online_update_health.update(status="error", failed_updates=2,
                                    last_error="prior failure")

    ml.record_outcome(signal_id, "win")

    health = ml.summary()["online_update_health"]
    assert health["status"] == "ok"
    assert health["last_error"] is None
    assert health["failed_updates"] == 2
    assert health["successful_updates"] == 1


def test_failed_online_fit_still_requests_batch_on_fifth_label(isolated, monkeypatch):
    signal_id, _ = isolated
    ml._model_online = Estimator("raise")
    ml._labeled_count = 19
    requested = []
    monkeypatch.setattr(ml, "_request_retrain", lambda: requested.append(True))

    ml.record_outcome(signal_id, "win")

    assert ml._labeled_count == 20
    assert requested == [True]
    assert ml.summary()["online_update_health"]["failed_updates"] == 1


def test_learning_health_is_restored_on_restart(isolated):
    _, path = isolated
    saved = dict(ml._online_update_health, status="error", failed_updates=3,
                 last_signal_id=42, last_error="ValueError: failed")
    joblib.dump({"version": ml._version, "labeled_count": 18,
                 "online_update_health": saved}, path / "re_ml_meta.pkl")

    ml.init(str(path))

    assert ml.summary()["online_update_health"] == saved


def test_legacy_metadata_does_not_invent_successful_updates(isolated):
    _, path = isolated
    ml._online_update_health.update(status="ok", successful_updates=99)
    joblib.dump({"version": ml._version, "labeled_count": 18},
                path / "re_ml_meta.pkl")

    ml.init(str(path))

    health = ml.summary()["online_update_health"]
    assert health["status"] == "unknown"
    assert health["successful_updates"] == 0
    assert health["failed_updates"] == 0


def test_untrainable_outcome_does_not_count_as_an_update(isolated):
    signal_id, _ = isolated
    repo.get_db().run("UPDATE re_signals SET net_pnl_dollars=NULL WHERE id=?", signal_id)

    ml.record_outcome(signal_id, "win")

    assert ml._labeled_count == 18
    assert ml.summary()["online_update_health"]["status"] == "unknown"
    assert ml.summary()["online_update_health"]["failed_updates"] == 0


def test_first_update_trains_real_sgd_and_reports_success(isolated):
    signal_id, path = isolated
    ml._model_online = None

    ml.record_outcome(signal_id, "win")

    assert ml._model_online.n_features_in_ == len(ml.FEATURE_NAMES)
    assert ml.summary()["online_update_health"]["successful_updates"] == 1
    assert np.isfinite(ml.predict([0.1] * len(ml.FEATURE_NAMES)))
    assert joblib.load(path / "re_ml_online.pkl").t_ == 2


def test_summary_health_is_a_snapshot_not_mutable_internal_state(isolated):
    health = ml.summary()["online_update_health"]

    health["failed_updates"] = 100

    assert ml.summary()["online_update_health"]["failed_updates"] == 0
