"""A handed-over model must still be handed over after a restart, and the
online learner must not be stranded on the old width.

`ml_handover` keeps the previous version's model scoring its leading features
until the first retrain. Two gaps were left:

1. The truncation width lived only in memory, but `_save_all` (called by
   `record_ref_signal` and `record_outcome` during the handover) stamped the
   meta file with the NEW version. On the next start the version matched, the
   33-wide models loaded as if current, no width was set, and both raised on a
   38-wide vector. `predict()` swallows that and returns None, and a None
   prediction does not block: the gate failed open until the next retrain,
   which is what `ml_handover` exists to prevent.

2. A retrain ends the handover for the batch model only. The online SGD model
   stayed 33 wide, so every later `predict` and `partial_fit` on it raised and
   was swallowed, for good: it was never replaced.

Real sklearn models, a temp data dir. Nothing here reaches a broker.
"""
from __future__ import annotations

import warnings
from unittest import mock

import joblib
import numpy as np
import pytest
from sklearn.linear_model import SGDRegressor

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine import ml_handover as ho

WIDTH = len(ml.FEATURE_NAMES)
OLD = 33


def _sgd(width):
    m = SGDRegressor(loss="huber", epsilon=0.1, random_state=42)
    rng = np.random.default_rng(0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.partial_fit(rng.random((20, width)), rng.standard_normal(20))
    return m


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name, value in [("_data_dir", None), ("_model_batch", None),
                        ("_model_online", None), ("_labeled_count", 0),
                        ("_train_history", []), ("_ref_level_stats", {})]:
        monkeypatch.setattr(ml, name, value)
    monkeypatch.setattr(ho, "legacy_width", None)
    yield


def _write(data_dir, version, batch, online):
    joblib.dump({"version": version, "labeled_count": 10, "train_history": [],
                 "ref_level_stats": {}}, data_dir / "re_ml_meta.pkl")
    if batch is not None:
        joblib.dump(batch, data_dir / "re_ml_batch.pkl")
    if online is not None:
        joblib.dump(online, data_dir / "re_ml_online.pkl")


class TestARestartMidHandover:
    def test_the_old_models_still_score_after_the_meta_was_restamped(self, tmp_path):
        """The meta already says the current version (a save happened during
        the handover) but the models on disk are still 33 wide."""
        _write(tmp_path, ml._version, _sgd(OLD), _sgd(OLD))

        ml.init(str(tmp_path))

        assert ml.predict([0.5] * WIDTH) is not None, \
            "the gate would fail open: no prediction from the handed-over models"

    def test_the_whole_sequence_bump_save_restart(self, tmp_path):
        _write(tmp_path, "re_ml_v8", _sgd(OLD), _sgd(OLD))
        ml.init(str(tmp_path))
        ml.record_ref_signal("round_10")          # restamps the meta

        for name in ("_model_batch", "_model_online"):
            setattr(ml, name, None)
        ho.set_legacy_width(None)
        ml.init(str(tmp_path))                    # the restart

        assert ml.predict([0.5] * WIDTH) is not None

    def test_a_current_width_model_is_not_truncated(self, tmp_path):
        """Control: an ordinary start must not set a width."""
        _write(tmp_path, ml._version, _sgd(WIDTH), _sgd(WIDTH))
        ml.init(str(tmp_path))
        assert ho.legacy_width is None
        assert ml.predict([0.5] * WIDTH) is not None

    def test_a_handover_with_only_an_online_model_still_truncates(self, tmp_path):
        _write(tmp_path, "re_ml_v8", None, _sgd(OLD))
        ml.init(str(tmp_path))
        assert ml.predict([0.5] * WIDTH) is not None


class TestTheOnlineLearnerIsNotStranded:
    def test_a_retrain_drops_an_online_model_of_the_old_width(self, monkeypatch):
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        ml._model_online = _sgd(OLD)
        ho.set_legacy_width(OLD)

        ml._install_batch(_sgd(WIDTH), "lgb", 100, 0.0)

        online = ml._model_online
        assert online is None or online.n_features_in_ == WIDTH

    def test_a_retrain_keeps_an_online_model_of_the_right_width(self, monkeypatch):
        """Control: only a mismatched model is dropped."""
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        keep = _sgd(WIDTH)
        ml._model_online = keep

        ml._install_batch(_sgd(WIDTH), "lgb", 100, 0.0)

        assert ml._model_online is keep

    def test_an_outcome_reaches_an_online_model_left_at_the_old_width(self, monkeypatch):
        """However it got there, the next outcome must leave a learner that
        can read the current vector."""
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        monkeypatch.setattr(ml, "_labeled_count", 3)
        ml._model_online = _sgd(OLD)
        row = {"id": 1, "outcome": "win", "sl_dist": 5.0, "net_pnl_dollars": 10.0,
               "ml_features_json": "[" + ",".join(["0.5"] * WIDTH) + "]"}
        with mock.patch("backend.src.services.reversal_engine.reversal_engine_repo"
                        ".get_signal_by_id", return_value=row):
            ml.record_outcome(1, "win")

        assert ml._model_online.n_features_in_ == WIDTH
