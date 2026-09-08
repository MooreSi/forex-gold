"""A version bump must not leave the engine with no model.

The handover logic lives in `ml_handover.py` rather than `ml_engine.py`
because that file sits on the repo's 800-line ceiling with one line spare;
the behaviour asserted here is unchanged by where it lives.

data-inspect/003, and the owner's own words: "you shouldn't have lost your ml
capabilities". He is right, though not for the reason first assumed -- the
training DATA was never lost. `_collect_training_data` reads every closed
signal from the database on each retrain and right-pads older rows with
`_FEATURE_NEUTRAL`, so a bump costs the fitted model, not the history.

What a bump DOES cost is the model itself until the next retrain, and that
window is dangerous because **the ML gate fails OPEN**:

    if fresh_prob is not None and float(fresh_prob) < _ML_BLOCK_THRESHOLD:

`predict()` returns None when no model is loaded, so every signal passes the
gate unfiltered until training completes. On 2026-09-05 v9 discarded the v8
models and the first v9 retrain was 2026-09-07 09:27.

Handover fixes both: keep serving the previous model, scoring the leading
features it was fitted on, until a retrain replaces it. Features are
append-only (`_FEATURE_NEUTRAL`'s contract), so the first N of a v9 vector are
exactly a v8 vector.

**Only from v5 onwards.** v5 replaced the LABEL -- was `rr_tp1 if win else
-1.0`, now realised net R -- so a v4 model predicts on a different scale
entirely and handing over from it would feed the gate numbers that mean
something else. That is the one thing this must refuse to do.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine import ml_handover as ho


class _FakeModel:
    """Records the width it was asked to predict on."""

    def __init__(self, n_features):
        self.n_features_in_ = n_features
        self.seen_widths = []

    def predict(self, arr):
        self.seen_widths.append(arr.shape[1])
        return [0.25]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(ml, "_model_batch", None)
    monkeypatch.setattr(ml, "_model_online", None)
    monkeypatch.setattr(ho, "legacy_width", None)
    yield


class TestHandoverIsAllowedWhenTheLabelIsUnchanged:
    @pytest.mark.parametrize("old", ["re_ml_v5", "re_ml_v6", "re_ml_v7", "re_ml_v8"])
    def test_a_same_label_version_may_hand_over(self, old):
        assert ho.may_hand_over(old) is True

    @pytest.mark.parametrize("old", ["re_ml_v2", "re_ml_v3", "re_ml_v4"])
    def test_a_pre_label_change_version_may_NOT(self, old):
        """v5 replaced the label. An older model predicts a different
        quantity, and the gate compares it to zero."""
        assert ho.may_hand_over(old) is False

    @pytest.mark.parametrize("junk", ["", "re_ml_", "nonsense", None, "re_ml_vX"])
    def test_anything_unparseable_may_NOT(self, junk):
        """Refusing is the safe direction: no handover just means the current
        behaviour, a retrain from scratch."""
        assert ho.may_hand_over(junk) is False


class TestTheOldModelKeepsScoringUntilAReplacementArrives:
    def test_a_narrower_model_is_given_only_the_features_it_knows(self):
        """The whole mechanism. A v8 model wants 33; a v9 vector is 38."""
        model = _FakeModel(33)
        ml._model_batch = model
        ho.set_legacy_width(33)

        out = ml.predict([0.5] * 38)

        assert out is not None, "the handed-over model produced no prediction"
        assert model.seen_widths == [33], f"got width {model.seen_widths}"

    def test_a_full_width_model_is_not_truncated(self):
        """Negative control: once a real v9 model exists it must see all 38."""
        model = _FakeModel(38)
        ml._model_batch = model
        ho.set_legacy_width(None)

        ml.predict([0.5] * 38)

        assert model.seen_widths == [38]

    def test_a_vector_shorter_than_the_model_is_not_padded_here(self):
        """Padding belongs in _collect_training_data, which knows the neutral
        for each feature. Silently zero-padding at predict time would score a
        signal against values that mean something."""
        model = _FakeModel(38)
        ml._model_batch = model
        ho.set_legacy_width(33)

        ml.predict([0.5] * 30)

        assert model.seen_widths == [30]
        # Note: `features[:33]` on a 30-item list returns all 30, so dropping
        # the `len(features) > _legacy_width` guard is an EQUIVALENT mutation.
        # It is kept because it states the intent -- truncate, never pad -- and
        # it is recorded here rather than chased with a test that cannot exist.


class TestTheGateIsNotLeftOpen:
    def test_predict_returns_a_number_while_handed_over(self):
        """The reason this matters. reversal_engine_live_execute blocks only
        `if fresh_prob is not None and < 0`, so a None prediction means the
        ML gate does not block AT ALL -- a bump would otherwise leave live
        execution unfiltered until the first retrain."""
        ml._model_batch = _FakeModel(33)
        ho.set_legacy_width(33)

        assert ml.predict([0.5] * 38) is not None

    def test_with_no_model_at_all_it_still_returns_none(self):
        """Unchanged: nothing to hand over from is still nothing."""
        assert ml.predict([0.5] * 38) is None


class TestHandoverEndsWhenARealModelArrives:
    def test_a_retrain_clears_the_truncation(self):
        """Without this, handover is permanent: the new full-width model would
        be fed only the leading block forever, and every macro feature v9 was
        added FOR would be invisible to it. The truncation must end the moment
        a model fitted on the current schema exists.
        """
        ho.set_legacy_width(33)

        ho.end()

        assert ho.legacy_width is None

    def test_the_new_model_then_sees_every_feature(self):
        ml._model_batch = _FakeModel(38)
        ho.set_legacy_width(33)
        ho.end()

        ml.predict([0.5] * 38)

        assert ml._model_batch.seen_widths == [38]
