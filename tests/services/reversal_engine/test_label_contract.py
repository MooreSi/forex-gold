"""Label provenance in private SQLite; fake candle history only."""
from dataclasses import asdict
import pytest
from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import tpl_label_repo as labels
from backend.src.services.reversal_engine import entry_study as study


@pytest.fixture
def re_store(tmp_path):
    repo.init(str(tmp_path / "labels.db"))
    yield repo
    repo.close_db()


def test_template_label_saves_policy_cost_and_true_availability(re_store):
    sid = repo.create_signal({"signal_ref": "RE-policy", "created_at": 100})
    labels.store_tpl([(sid, 0.2)], contract={"policy": asdict(study.template_policy(0.575)),
                                          "cost_pts": 0.575, "source": "fixed_template_m1_v1"},
                     available_at=1000)
    row = labels.contract_for(sid)
    assert row["cost_pts"] == 0.575
    assert row["available_at"] == 1000
    assert row["source"] == "fixed_template_m1_v1"
    assert len(row["policy_hash"]) == 64
    assert repo.get_signal_by_id(sid)["tpl_r"] == 0.2


def test_legacy_label_does_not_invent_policy_provenance(re_store):
    sid = repo.create_signal({"signal_ref": "RE-legacy", "created_at": 100})
    labels.store_tpl([(sid, 0.2)])
    assert labels.contract_for(sid) is None
