"""Finite scores only; fake estimators cannot reach the broker."""
import pytest
from backend.src.services.reversal_engine import edge_model as edge
from backend.src.services.reversal_engine import ml_engine as ml


class Prediction:
    def __init__(self, value):
        self.value = value
    def predict(self, X):
        return [self.value]


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_batch_prediction_is_no_opinion(monkeypatch, bad):
    monkeypatch.setattr(ml, "_model_batch", Prediction(bad))
    monkeypatch.setattr(ml, "_model_online", None)
    assert ml.predict([0.1]*len(ml.FEATURE_NAMES)) is None
    assert ml.summary()["prediction_health"]["status"] == "unavailable"


def test_healthy_prediction_reports_healthy_status(monkeypatch):
    monkeypatch.setattr(ml, "_model_batch", Prediction(0.2))
    monkeypatch.setattr(ml, "_model_online", None)
    assert ml.predict([0.1]*len(ml.FEATURE_NAMES)) == 0.2
    assert ml.summary()["prediction_health"]["status"] == "ok"


def test_proven_edge_refuses_nan_score():
    model = edge.EdgeModel()
    model._model = Prediction(float("nan"))
    model.status = edge.EdgeStatus(fitted=True, proven=True)
    take, reason, prediction = model.decide([0.1]*len(ml.FEATURE_NAMES))
    assert take is False
    assert "finite" in reason
    assert prediction is None
