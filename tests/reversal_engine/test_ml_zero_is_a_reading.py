"""A feature whose real value is 0.0 must reach the model as 0.0.

`extract_features` used to read most inputs as `float(x or default)`. That
swaps a genuine zero for the default, and for several features the zero is
the most informative reading there is:

    regime_score          0.0 = ranging (ADX < 14)   -> became 0.5, "volatile"
    news_proximity_norm   0.0 = event imminent       -> became 1.0, "safe"
    fvg_dist_norm         0.0 = at the gap           -> became 5.0, "no gap"
    minutes_since_last_ref 0  = REF just posted      -> became 240, least due
    distance_pts          0   = price at the level   -> became 5 points

Measured on the owner's Mac on 2026-10-01: 1,525 stored signals had ADX < 14,
and not one of 7,232 stored vectors carried regime_score 0.0.

The second half is the training data. Those 7,232 vectors were stored with the
substituted values, and two of the substitutions can be undone from columns
the same row still holds: regime_score from `adx`, distance from
`price_at_signal` and `level_price`. Without that the retrained model would see
"ranging" as 0.5 in 7,000 old rows and 0.0 in the new ones.

Nothing here reaches a broker.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from backend.src.services.reversal_engine import ml_engine as ml
from backend.src.services.reversal_engine.ml_engine import _training_data as td

N = ml.FEATURE_NAMES


def _at(vec, name):
    return vec[N.index(name)]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    # Neither research model may be consulted: they are not under test.
    monkeypatch.setattr(ml, "learning_from_ref_enabled", lambda: False)
    monkeypatch.setattr(ml, "_ref_level_stats", {})


def _base(**over):
    sig = {"direction": "BUY", "level_type": "round_10", "level_score": 0.6,
           "atr": 10.0, "adx": 25.0, "created_at": 1_790_000_000.0,
           "price_at_signal": 3302.0, "level_price": 3300.0}
    sig.update(over)
    return sig


class TestAZeroIsKept:
    @pytest.mark.parametrize("key,name,expected", [
        ("regime_score", "regime_score", 0.0),
        ("news_proximity_norm", "news_proximity_norm", 0.0),
        ("fvg_dist_norm", "fvg_dist_norm", 0.0),
        ("ref_discipline_score", "ref_discipline_score", 0.0),
        ("ref_aggression_score", "ref_aggression_score", 0.0),
        ("minutes_since_last_ref", "minutes_since_ref_norm", 0.0),
        ("level_score", "level_score", 0.0),
    ])
    def test_a_zero_input_is_a_zero_feature(self, key, name, expected):
        v = ml.extract_features(_base(**{key: 0.0}))
        assert _at(v, name) == expected

    def test_a_signal_sitting_on_its_level_is_zero_away(self):
        v = ml.extract_features(_base(price_at_signal=3300.0, level_price=3300.0))
        assert _at(v, "distance_norm") == 0.0

    def test_an_explicit_zero_distance_is_zero_away(self):
        v = ml.extract_features(_base(distance_pts=0.0))
        assert _at(v, "distance_norm") == 0.0


class TestAbsentStillMeansTheDocumentedNeutral:
    """Negative control: the fix must only change zeros, not absences."""

    @pytest.mark.parametrize("name,neutral", [
        ("regime_score", 0.5), ("news_proximity_norm", 1.0),
        ("fvg_dist_norm", 5.0), ("ref_discipline_score", 0.5),
        ("ref_aggression_score", 0.5), ("minutes_since_ref_norm", 1.0),
        ("fvg_fresh", 0.5),
    ])
    def test_a_missing_input_is_the_neutral(self, name, neutral):
        assert _at(ml.extract_features(_base()), name) == neutral

    @pytest.mark.parametrize("name,neutral", [
        ("regime_score", 0.5), ("news_proximity_norm", 1.0),
        ("fvg_dist_norm", 5.0), ("minutes_since_ref_norm", 1.0),
    ])
    def test_an_explicit_none_is_the_neutral(self, name, neutral):
        key = {"minutes_since_ref_norm": "minutes_since_last_ref"}.get(name, name)
        assert _at(ml.extract_features(_base(**{key: None})), name) == neutral

    def test_a_missing_price_still_falls_back_to_five_points(self):
        v = ml.extract_features(_base(price_at_signal=None, level_price=None))
        assert _at(v, "distance_norm") == pytest.approx(5.0 / 10.0)

    def test_a_non_zero_value_is_unchanged(self):
        v = ml.extract_features(_base(regime_score=0.75, news_proximity_norm=0.4))
        assert _at(v, "regime_score") == 0.75
        assert _at(v, "news_proximity_norm") == 0.4


def _stored_row(vec, **cols):
    row = {"id": 1, "outcome": "win", "sl_dist": 5.0, "net_pnl_dollars": 10.0,
           "live_exec_status": None, "adx": 25.0, "atr": 10.0,
           "price_at_signal": 3302.0, "level_price": 3300.0,
           "ml_features_json": json.dumps(vec)}
    row.update(cols)
    return row


def _train_on(rows):
    with mock.patch("backend.src.services.reversal_engine.reversal_engine_repo"
                    ".get_ml_training_data", return_value=rows):
        return td._get_training_data()


def _vector_as_stored_before_the_fix(**sig):
    """What the old extraction wrote: the substituted values."""
    v = ml.extract_features(_base(**sig))
    v[N.index("regime_score")] = 0.5 if _at(v, "regime_score") == 0.0 else _at(v, "regime_score")
    if _at(v, "distance_norm") == 0.0:
        v[N.index("distance_norm")] = 5.0 / max(float(sig.get("atr", 10.0)), 1.0)
    return v


class TestTheStoredHistoryIsRepairedWhereTheRowCanSay:
    def test_a_ranging_signal_stored_as_volatile_trains_as_ranging(self):
        vec = _vector_as_stored_before_the_fix(adx=10.0, regime_score=0.0)
        X, _ = _train_on([_stored_row(vec, adx=10.0)])
        assert _at(X[0], "regime_score") == 0.0

    def test_a_genuinely_mixed_regime_is_left_alone(self):
        """Control: ADX 22 really is 0.5."""
        vec = _vector_as_stored_before_the_fix(adx=22.0, regime_score=0.5)
        X, _ = _train_on([_stored_row(vec, adx=22.0)])
        assert _at(X[0], "regime_score") == 0.5

    def test_a_row_with_no_adx_is_left_alone(self):
        vec = _vector_as_stored_before_the_fix(adx=10.0, regime_score=0.0)
        X, _ = _train_on([_stored_row(vec, adx=None)])
        assert _at(X[0], "regime_score") == 0.5

    def test_a_signal_stored_five_points_away_from_its_own_level_trains_at_zero(self):
        vec = _vector_as_stored_before_the_fix(price_at_signal=3300.0)
        X, _ = _train_on([_stored_row(vec, price_at_signal=3300.0)])
        assert _at(X[0], "distance_norm") == 0.0

    def test_a_real_five_point_distance_is_left_alone(self):
        vec = _vector_as_stored_before_the_fix(price_at_signal=3305.0)
        X, _ = _train_on([_stored_row(vec, price_at_signal=3305.0)])
        assert _at(X[0], "distance_norm") == pytest.approx(0.5)


class TestTheOnlineLearnerReadsTheSameRows:
    """`record_outcome` used to demand the exact current width, so the online
    model skipped every older row the batch model trains on, and it never saw
    the repair above."""

    def _record(self, monkeypatch, row):
        seen = []

        class _Online:
            def partial_fit(self, X, y):
                seen.append(list(X[0]))

        monkeypatch.setattr(ml, "_model_online", _Online())
        monkeypatch.setattr(ml, "_labeled_count", 3)
        monkeypatch.setattr(ml, "_save_all", lambda: None)
        with mock.patch("backend.src.services.reversal_engine.reversal_engine_repo"
                        ".get_signal_by_id", return_value=row):
            ml.record_outcome(1, "win")
        return seen

    def test_a_shorter_older_vector_is_padded_not_skipped(self, monkeypatch):
        vec = ml.extract_features(_base())[:33]
        seen = self._record(monkeypatch, _stored_row(vec))
        assert len(seen) == 1 and len(seen[0]) == len(N)

    def test_the_regime_repair_reaches_it(self, monkeypatch):
        vec = _vector_as_stored_before_the_fix(adx=10.0, regime_score=0.0)
        seen = self._record(monkeypatch, _stored_row(vec, adx=10.0))
        assert _at(seen[0], "regime_score") == 0.0
