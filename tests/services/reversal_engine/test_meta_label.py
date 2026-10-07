"""Exact decision features and causal learning; all models are local fakes."""
import json
import numpy as np
from unittest.mock import patch
from backend.src.services.reversal_engine import meta_label as ml
from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES


class Scorer:
    uses_xasset = False
    status = ml.MetaLabelStatus(True, 200, 4, 0.8, 0.9)

    def probability(self, features):
        self.seen = features
        return 0.8


def test_meta_scores_exact_stored_creation_vector_without_rebuilding_enrichment():
    vector = [0.1]*len(FEATURE_NAMES)
    vector[FEATURE_NAMES.index("fvg_dist_norm")] = 0.0
    scorer = Scorer()
    with patch.object(ml, "get_instance", return_value=scorer):
        assert ml.score_signal({"ml_features_json": json.dumps(vector)}) == 0.8
    assert scorer.seen == vector


def test_meta_without_recorded_features_has_no_opinion():
    with patch.object(ml, "get_instance", return_value=Scorer()):
        assert ml.score_signal({"direction": "BUY"}) is None


def test_high_win_probability_with_negative_payoff_does_not_arm():
    rows = [{"features": [float(i%10 < 9)], "realised_r": 0.01 if i%10 < 9 else -2,
             "cost_r": 0, "open_time": i*10000, "close_time": i*10000+50}
            for i in range(400)]
    # Uninformative forecast that exceeds the probability threshold can have
    # excellent AUC on a different population and still lose in this one.
    class Overconfident:
        def fit(self, X, y, sample_weight):
            return self
        def predict_proba(self, X):
            return np.asarray([[0.1, 0.9 if x[0] else 0.8] for x in X])
    model = ml.MetaLabeller(min_samples=100)
    with patch.object(model, "_new_model", return_value=Overconfident()):
        status = model.fit(rows)
    assert status.ready is False
    assert "payoff" in status.refusal


def test_meta_training_and_scoring_read_historical_repairs_identically():
    vector = [0.1]*len(FEATURE_NAMES)
    vector[FEATURE_NAMES.index("regime_score")] = 0.5
    row = {"ml_features_json": json.dumps(vector), "adx": 10, "atr": 8,
           "created_at": 100, "trigger_time": 110, "close_time": 200,
           "outcome": "win", "sl_dist": 5, "net_pnl_dollars": 10}
    trained = ml.rows_from_signals([row])[0]["features"]
    scorer = Scorer()
    with patch.object(ml, "get_instance", return_value=scorer):
        ml.score_signal(row)
    assert trained == scorer.seen
    assert trained[FEATURE_NAMES.index("regime_score")] == 0
