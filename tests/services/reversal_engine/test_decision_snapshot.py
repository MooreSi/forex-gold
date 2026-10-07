"""Snapshots use private SQLite and fake templates; no broker calls."""
import json
import pytest
from backend.src.services.reversal_engine import reversal_engine_repo as repo


@pytest.fixture
def re_store(tmp_path):
    repo.init(str(tmp_path / "reversal.db"))
    yield repo
    repo.close_db()


def test_reversal_schema_has_decision_snapshot_storage(re_store):
    assert re_store.get_db().all("SELECT signal_ref FROM re_decision_snapshots") == []


def test_candidate_pool_and_fill_vector_are_persisted(re_store, monkeypatch):
    from backend.src.services.reversal_engine import decision_snapshot as ds
    from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES
    monkeypatch.setattr(ds, "_policy", lambda strategy: {"strategy": strategy, "template": {"sl_pips": 50}})
    v = [0.1]*len(FEATURE_NAMES)
    selected = {"signal_ref": "RE-selected", "strategy": "template:fixed", "created_at": 100}
    other = {"signal_ref": "RE-other", "created_at": 100}
    ds.record_candidates(selected, [({}, "BUY", other, v, -0.1), ({}, "BUY", selected, v, 0.2)])
    ds.record_fill(selected, v, 0.3, 200)
    rows = ds.snapshots("RE-selected")
    assert [(r["stage"], r["chosen"]) for r in rows] == [("creation", 0), ("creation", 1), ("fill", 1)]
    assert json.loads(rows[-1]["features_json"]) == v
    assert rows[-1]["decision_ts"] == 200
    assert rows[-1]["schema_hash"] == rows[0]["schema_hash"]
    assert rows[-1]["policy_hash"] == rows[0]["policy_hash"]


def test_bad_vector_is_recorded_as_unavailable_not_as_a_zero_prediction(re_store):
    from backend.src.services.reversal_engine import decision_snapshot as ds
    ds.record_fill({"signal_ref": "RE-bad"}, [float("nan")], float("inf"), 100)
    row = ds.snapshots("RE-bad")[0]
    assert row["features_json"] is None
    assert row["predicted_r"] is None
    assert row["health"] == "invalid_features"
