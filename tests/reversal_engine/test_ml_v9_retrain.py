"""The v9 vector actually trains, and what the macro columns can tell us yet.

Spec: docs/todo/001-reversal-macro-context.md. No real model file is written
outside tmp_path, no DB is touched, nothing places or modifies a trade.

Two separate things here.

**That it trains at all.** Bumping `_version` discards the fitted v8 models, so
the first thing that happens in production is a retrain over the back-filled
history at the new width. Every other test in this directory checks feature
*extraction*; none of them would notice `_retrain()` raising on a 38-wide
matrix or `predict()` refusing the vector afterwards.

**What the importances can and cannot say.** Spec section 6 asks where the five
macro features rank after the first retrain. Immediately after the bump the
answer is fixed in advance and means nothing: every historical row is
back-filled with the same `_FEATURE_NEUTRAL` constant, and a column with no
variance cannot be split on. The ranking only becomes evidence once enough
signals have been created carrying real macro. `test_constant_macro_columns_
carry_no_importance` pins that so the zero is read as "not measurable yet"
rather than "macro does not help".
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.reversal_engine import ml_engine as m
from backend.src.services.reversal_engine import re_macro


def _row(width, net, seed):
    """A closed signal whose stored vector is `width` wide. The non-macro
    slots vary so the model has something real to fit."""
    feats = [round(0.1 + (seed % 7) * 0.05 + i * 0.001, 4) for i in range(width)]
    return {
        "ml_features_json": json.dumps(feats),
        "outcome": "win" if net > 0 else "loss",
        "sl_dist": 7.0,
        "net_pnl_dollars": net,
    }


def _history(width, n=40):
    return [_row(width, 30.0 if i % 3 else -70.0, i) for i in range(n)]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Model state and output directory confined to this test."""
    monkeypatch.setattr(m, "_data_dir", tmp_path)
    monkeypatch.setattr(m, "_model_batch", None)
    monkeypatch.setattr(m, "_model_online", None)
    monkeypatch.setattr(m, "_labeled_count", 0)
    monkeypatch.setattr(m, "_train_history", [])
    return tmp_path


def _load_from(monkeypatch, rows):
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    monkeypatch.setattr(re_db, "get_ml_training_data", lambda: rows)


class TestTheV9RoundTrip:
    def test_a_fully_backfilled_v8_history_trains(self, isolated, monkeypatch):
        """The real first-run-after-the-bump case: every stored vector is 33
        wide and gets right-padded to 38 before it reaches the model."""
        _load_from(monkeypatch, _history(33))
        m._retrain()
        assert m._model_batch is not None
        assert m._labeled_count == 40
        assert m._model_batch.n_features_in_ == 38

    def test_predict_accepts_a_live_38_wide_vector_after_that_retrain(
        self, isolated, monkeypatch
    ):
        """The dimension mismatch a missed _version bump would produce, caught
        on the path that gates live execution."""
        _load_from(monkeypatch, _history(33))
        m._retrain()
        feats = m.extract_features({"level_type": "asia_low", "vix_level": 30.0})
        assert len(feats) == 38
        assert isinstance(m.predict(feats), float)

    def test_a_mixed_width_history_trains(self, isolated, monkeypatch):
        """Once real v9 rows accumulate the history is mixed. Padding happens
        per row, so both widths must sit in the same matrix."""
        _load_from(monkeypatch, _history(33, 20) + _history(38, 20))
        m._retrain()
        assert m._model_batch.n_features_in_ == 38
        assert m._labeled_count == 40

    def test_too_few_rows_leaves_the_model_untrained(self, isolated, monkeypatch):
        """Negative control. If this passed with a model fitted, the two tests
        above would prove nothing about the data reaching the fit."""
        _load_from(monkeypatch, _history(33, m._MIN_TRAIN - 1))
        m._retrain()
        assert m._model_batch is None


class TestWhatTheImportancesCanSayYet:
    def test_constant_macro_columns_carry_no_importance(self, isolated, monkeypatch):
        """Not a claim about gold. Every back-filled row carries the identical
        neutral in these five slots, so the split gain is zero by construction.
        A zero here after the first retrain is 'no data yet', and reporting it
        as 'macro does not help' would be wrong."""
        _load_from(monkeypatch, _history(33))
        m._retrain()
        importances = list(m._model_batch.feature_importances_)
        macro = importances[-5:]
        assert set(macro) == {0}, (
            "a constant column was split on, which would mean the back-fill "
            "is not constant after all"
        )

    def test_a_varying_macro_column_can_be_used(self, isolated, monkeypatch):
        """The other half of the control: the zero above is a property of the
        data, not of the wiring. Give one macro slot real variance and the
        model reaches for it."""
        rows = _history(38)
        for i, r in enumerate(rows):
            feats = json.loads(r["ml_features_json"])
            vix = m.FEATURE_NAMES.index("vix_level")
            # Make vix_level track the label so it is genuinely informative.
            feats[vix] = 0.9 if r["outcome"] == "win" else 0.1
            r["ml_features_json"] = json.dumps(feats)
        _load_from(monkeypatch, rows)
        m._retrain()
        vix = m.FEATURE_NAMES.index("vix_level")
        assert m._model_batch.feature_importances_[vix] > 0


def test_the_macro_slots_are_the_last_five_columns_of_the_matrix(
    isolated, monkeypatch
):
    """The two tests above index the matrix by position. If FEATURE_NAMES ever
    stopped being append-only, they would silently measure other columns."""
    _load_from(monkeypatch, _history(33, 20))
    X, _ = m._get_training_data()
    assert len(X[0]) == len(m.FEATURE_NAMES)
    assert m.FEATURE_NAMES[-5:] == re_macro.MACRO_FEATURE_NAMES
