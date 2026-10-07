"""Live path with fake candles and no main engine; no order can be sent."""
import json
from unittest.mock import AsyncMock
from backend.src.services.reversal_engine import edge_model as edge
from backend.src.services.reversal_engine import ml_engine as ml
from tests.reversal_engine.test_ml_fill_time_rescore import _run, stored, scored


def test_creation_trained_edge_receives_creation_features_at_execution(monkeypatch, stored, scored):
    observed = []
    vector = [0.2]*len(ml.FEATURE_NAMES)
    monkeypatch.setattr("backend.src.services.reversal_engine.re_macro.get_cycle_context", AsyncMock(return_value={}))
    monkeypatch.setattr("backend.src.services.risk.capability_gates.require_proven_edge", lambda rs: True)
    monkeypatch.setattr(edge, "decide", lambda f: (observed.append(f) or False, "test refusal", None))
    _run(ml_features_json=json.dumps(vector))
    assert observed == [vector]
