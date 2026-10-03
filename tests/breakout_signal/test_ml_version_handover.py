"""A Breakout ML version bump must not leave the engine without its old model.

`MODEL_VERSION` is `N_FEATURES`, and `_model_path` embeds it, so the first
start after a feature is appended finds no file for the new version. Until
now that meant: no batch model, no online model, and `has_batch()` False, so
the live-execution gate (`ml_prob is not None and bo_ml.has_batch()`) did not
block anything. `init()` retrains from the database at once, which closes the
window only when the retrain succeeds and there are `MIN_TRAIN_SAMPLES`
closed rows. The online SGD learner was always lost.

The Reversal Engine solved the same thing with `ml_handover` (2026-09-08).
This is the Breakout version. Features are append-only (`_pad_legacy`'s
contract), so the first N of a new vector are exactly an old vector, and the
previous model keeps scoring on that leading block until a retrain replaces it.

Three ways handover goes wrong silently, each pinned below:
  * predict() hands a 21-wide model a 22-wide row; the broad `except` in
    predict() swallows the shape error and returns None, i.e. gate open.
  * `_save_all` writes the 21-wide model under the 22 file name; the next
    restart loads it with no handover state and does the same.
  * `_online_update` feeds the old SGD pipeline full-width rows, which fail
    in `partial_fit` and are logged at debug level.
"""
from __future__ import annotations

import json

import joblib
import numpy as np
import pytest

from backend.src.services.breakout_signal import ml_engine as ml
from backend.src.services.breakout_signal import ml_handover as ho

pytestmark = pytest.mark.skipif(not ml._ML_AVAILABLE, reason="ml deps missing")

NEW = ml.N_FEATURES            # the current width
OLD = NEW - 1                  # the previous version's width


def _batch(width, seed=0):
    from sklearn.ensemble import RandomForestRegressor
    rng = np.random.RandomState(seed)
    X = rng.rand(30, width)
    y = rng.rand(30) - 0.5
    return RandomForestRegressor(n_estimators=5, random_state=0).fit(X, y)


def _online(width, seed=0):
    from sklearn.linear_model import SGDRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    rng = np.random.RandomState(seed)
    pipe = Pipeline([("scaler", StandardScaler()),
                     ("reg", SGDRegressor(loss="huber", epsilon=0.1, max_iter=1,
                                          warm_start=True, random_state=42))])
    for _ in range(5):
        x = rng.rand(1, width)
        pipe.named_steps["scaler"].partial_fit(x)
        pipe.named_steps["reg"].partial_fit(
            pipe.named_steps["scaler"].transform(x), np.array([0.1]))
    return pipe


def _write_version(data_dir, version, *, batch=True, online=True, meta=True):
    if meta:
        joblib.dump({"labeled_count": 40, "version": version},
                    data_dir / f"bo_ml_meta_v{version}.joblib")
    if batch:
        joblib.dump(_batch(version), data_dir / f"bo_ml_batch_v{version}.joblib")
    if online:
        joblib.dump(_online(version), data_dir / f"bo_ml_online_v{version}.joblib")


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "_model", None)
    monkeypatch.setattr(ml, "_model_online", None)
    monkeypatch.setattr(ml, "_data_dir", tmp_path)
    monkeypatch.setattr(ml, "_labeled_count", 0)
    monkeypatch.setattr(ml, "_new_since_retrain", 0)
    monkeypatch.setattr(ho, "legacy_width", None)
    yield


class TestFindingThePreviousModel:
    def test_the_highest_older_version_wins(self, tmp_path):
        _write_version(tmp_path, OLD - 2)
        _write_version(tmp_path, OLD)
        batch, online, width = ho.hand_over(tmp_path, NEW)
        assert width == OLD
        assert batch.n_features_in_ == OLD

    def test_the_current_and_newer_versions_are_never_handed_over(self, tmp_path):
        """A newer file means this is a rollback; a same-version file is not a
        handover at all. Neither may be served truncated."""
        _write_version(tmp_path, NEW)
        _write_version(tmp_path, NEW + 3)
        assert ho.hand_over(tmp_path, NEW) == (None, None, None)

    def test_nothing_on_disk_means_nothing_handed_over(self, tmp_path):
        assert ho.hand_over(tmp_path, NEW) == (None, None, None)

    def test_a_version_below_the_padding_floor_is_refused(self, tmp_path):
        """`_pad_legacy` refuses vectors under 15, so the history cannot be
        replayed against such a model either."""
        _write_version(tmp_path, ho.MIN_HANDOVER_VERSION - 1)
        assert ho.hand_over(tmp_path, NEW) == (None, None, None)

    def test_an_online_model_of_a_different_width_is_dropped(self, tmp_path):
        """Truncating one width for both would break the other."""
        joblib.dump(_batch(OLD), tmp_path / f"bo_ml_batch_v{OLD}.joblib")
        joblib.dump(_online(OLD - 1), tmp_path / f"bo_ml_online_v{OLD}.joblib")
        batch, online, width = ho.hand_over(tmp_path, NEW)
        assert batch is not None and online is None and width == OLD

    def test_a_lightgbm_booster_width_is_read(self):
        """`lgb.train` returns a Booster, which has no n_features_in_."""
        class _Booster:
            def num_feature(self):
                return 21
        assert ho.model_width(_Booster()) == 21

    def test_an_online_only_previous_version_is_handed_over(self, tmp_path):
        joblib.dump(_online(OLD), tmp_path / f"bo_ml_online_v{OLD}.joblib")
        batch, online, width = ho.hand_over(tmp_path, NEW)
        assert batch is None and online is not None and width == OLD


class TestLoadingAfterABump:
    def test_the_previous_model_is_loaded_and_the_gate_has_a_batch(self, tmp_path):
        _write_version(tmp_path, OLD)
        ml._load_all()
        assert ml.has_batch() is True, "gate would fail open: has_batch() is False"
        assert ho.legacy_width == OLD

    def test_a_current_version_model_is_loaded_normally(self, tmp_path):
        """Negative control: handover must not engage when the model is current."""
        _write_version(tmp_path, NEW)
        _write_version(tmp_path, OLD)
        ml._load_all()
        assert ml._model.n_features_in_ == NEW
        assert ho.legacy_width is None

    def test_no_files_at_all_stays_untrained(self):
        ml._load_all()
        assert ml.is_trained() is False and ho.legacy_width is None


class TestScoringWhileHandedOver:
    def test_predict_returns_a_number_for_a_full_width_row(self, tmp_path):
        """The reason this exists. A 21-wide model given 22 features raises, and
        predict() swallows it and returns None, which the gate reads as 'pass'."""
        _write_version(tmp_path, OLD)
        ml._load_all()
        out = ml.predict([0.5] * NEW)
        assert out is not None

    def test_the_old_models_see_exactly_the_leading_block(self):
        seen = []

        class _M:
            def predict(self, X):
                seen.append(X.shape[1])
                return [0.2]
        ml._model, ml._model_online = _M(), _M()
        ho.set_legacy_width(OLD)
        ml.predict([0.5] * NEW)
        assert seen == [OLD, OLD]

    def test_a_current_model_is_not_truncated(self):
        seen = []

        class _M:
            def predict(self, X):
                seen.append(X.shape[1])
                return [0.2]
        ml._model = _M()
        ml.predict([0.5] * NEW)
        assert seen == [NEW]

    def test_a_wrong_length_row_is_still_refused(self):
        ml._model = _batch(OLD)
        ho.set_legacy_width(OLD)
        assert ml.predict([0.5] * (NEW + 1)) is None


class TestTheOnlineLearnerKeepsLearning:
    def test_an_outcome_updates_the_handed_over_online_model(self):
        ml._model_online = _online(OLD)
        ho.set_legacy_width(OLD)
        before = ml._model_online.named_steps["scaler"].n_samples_seen_
        ml._online_update([0.4] * NEW, 1.0)
        after = ml._model_online.named_steps["scaler"].n_samples_seen_
        assert after == before + 1, "the update was swallowed by the shape error"


class TestNothingLegacyIsSavedUnderTheNewName:
    def test_save_while_handed_over_writes_no_current_version_files(self, tmp_path):
        _write_version(tmp_path, OLD)
        ml._load_all()
        ml._save_all()
        for name in ("meta", "batch", "online"):
            assert not (tmp_path / f"bo_ml_{name}_v{NEW}.joblib").exists(), name
        # and the previous version's files are untouched, so a restart hands
        # over again rather than loading a 21-wide model as if it were 22.
        assert (tmp_path / f"bo_ml_batch_v{OLD}.joblib").exists()

    def test_save_after_handover_ends_writes_the_new_files(self, tmp_path):
        ml._model = _batch(NEW)
        ml._save_all()
        assert (tmp_path / f"bo_ml_batch_v{NEW}.joblib").exists()


def _rows(n):
    return [{"ml_features_json": json.dumps([0.1 * (i % 7)] * NEW),
             "outcome": "win" if i % 2 else "loss", "rr_tp1": 1.5}
            for i in range(n)]


class TestARetrainEndsTheHandover:
    def test_a_successful_retrain_replaces_the_model_and_the_stale_online(
            self, monkeypatch, tmp_path):
        _write_version(tmp_path, OLD)
        ml._load_all()
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data", lambda: _rows(40))
        ml._retrain()
        assert ho.legacy_width is None
        assert ml._model_online is None, \
            "a 21-wide online model left in place would fail every full-width update"
        assert ml.predict([0.5] * NEW) is not None

    def test_a_retrain_with_too_little_data_keeps_the_handed_over_model(
            self, monkeypatch, tmp_path):
        _write_version(tmp_path, OLD)
        ml._load_all()
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        monkeypatch.setattr(bdb, "get_ml_training_data",
                            lambda: _rows(ml.MIN_TRAIN_SAMPLES - 1))
        ml._retrain()
        assert ho.legacy_width == OLD
        assert ml.has_batch() is True


class TestInitStillRebuildsAtOnce:
    def test_init_retrains_even_though_a_model_was_handed_over(
            self, monkeypatch, tmp_path):
        """init() used to retrain only when `_model is None`. A handed-over model
        makes that False, so without this the old model would serve until the
        next 5 outcomes instead of being replaced at startup."""
        _write_version(tmp_path, OLD)
        calls = []
        monkeypatch.setattr(ml, "_retrain", lambda: calls.append(1))
        ml.init(tmp_path)
        assert calls == [1]
