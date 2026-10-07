"""Local artifact isolation, no saved production model or provider calls."""
import json
from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
from backend.src.services.reversal_engine.evidence.experiments import run_candidates


def test_insufficient_evidence_is_tracked_and_restarts_do_not_duplicate_run(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.put("demo", {"source": "mt5", "kind": "broker_label", "key": "T1", "event_ts": 10,
        "available_at": 20, "payload": {"features": [0.] * 38, "external_features": [0.] * 13,
        "decision_ts": 10, "r": -.2, "policy_hash": "policy-A", "champion_prediction": .1}})
    result = run_candidates(store, "demo", tmp_path / "runs")
    assert len(result) == 1
    assert result[0]["metrics"]["eligible"] is False
    assert result[0]["model_hash"] is None
    artifact = tmp_path / "runs" / result[0]["id"] / "dataset.json"
    assert json.loads(artifact.read_text())[0]["r"] == -.2
    assert run_candidates(EvidenceStore(tmp_path / "e.db"), "demo", tmp_path / "runs") == []
    assert run_candidates(store, "live", tmp_path / "runs") == []


def test_unmeasured_cost_rows_are_retained_for_audit_but_cannot_train_a_model(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    for i in range(80):
        store.put("demo", {"source": "mt5", "kind": "broker_label", "key": str(i), "event_ts": i * 3600,
            "available_at": i * 3600 + 10, "payload": {"features": [0.] * 38, "external_features": [0.] * 13,
            "decision_ts": i * 3600, "r": -.2, "policy_hash": "policy-A", "champion_prediction": .1,
            "costs_complete": False}})
    result = run_candidates(store, "demo", tmp_path / "runs")
    assert result[0]["model_hash"] is None
    assert result[0]["metrics"]["missing_cost_rows"] == 80
    assert len(json.loads((tmp_path / "runs" / result[0]["id"] / "dataset.json").read_text())) == 80


def test_enough_measured_cost_rows_produce_a_separate_shadow_model(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    for i in range(80):
        store.put("demo", {"source": "mt5", "kind": "broker_label", "key": str(i), "event_ts": i * 3600,
            "available_at": i * 3600 + 10, "payload": {"features": [float(i)] * 38, "external_features": [0.] * 13,
            "decision_ts": i * 3600, "r": -.2, "policy_hash": "policy-A", "champion_prediction": .1,
            "costs_complete": True}})
    result = run_candidates(store, "demo", tmp_path / "runs")
    assert result[0]["model_hash"] is not None
    assert result[0]["metrics"]["eligible"] is False
    assert (tmp_path / "runs" / result[0]["id"] / "candidate.pkl").is_file()
