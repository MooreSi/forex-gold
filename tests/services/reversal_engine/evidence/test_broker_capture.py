"""Broker fixtures only; this collector has no order collaborators."""
import json
from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
from backend.src.services.reversal_engine.evidence.broker_capture import capture
from .test_observations import signal, trade


def snapshot():
    return {"id": 1, "signal_ref": "RE-A", "stage": "fill", "decision_ts": 19,
            "features_json": json.dumps([0.0] * 38), "policy_hash": "policy-A",
            "model_id": "champion", "predicted_r": 0.1}


def test_execution_label_and_partial_are_linked_and_not_duplicated(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    parts = [{"id": 1, "trade_id": "T1", "ts": 30, "pnl": 4, "lots_closed": .01,
              "close_price": 100, "reason": "TP1"}]
    capture(store, [signal()], [trade()], parts, [snapshot()], "demo", 100)
    capture(store, [signal()], [trade()], parts, [snapshot()], "demo", 200)
    labels = store.labels("demo")
    assert len(labels) == 1
    assert labels[0]["payload"]["r"] == .25
    assert labels[0]["available_at"] == 100
    assert labels[0]["payload"]["features"] == [0.0] * 38
    assert [e["payload"]["pnl"] for e in store.events("demo", 200) if e["kind"] == "broker_partial"] == [4]


def test_ambiguous_ticket_has_no_training_label(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    capture(store, [signal()], [trade(), {**trade(), "trade_id": "T2"}], [], [snapshot()], "demo", 100)
    assert store.labels("demo") == []


def test_post_execution_snapshot_is_not_used_as_training_features(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    capture(store, [signal()], [trade()], [], [{**snapshot(), "decision_ts": 25}], "demo", 100)
    assert store.labels("demo")[0]["payload"]["features"] is None


def test_measured_cashflow_label_includes_entry_commission_without_changing_ledger(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    deals = [{"entry": 0, "volume": .02, "profit": 0, "swap": 0, "fee": 0, "commission": -1},
             {"entry": 1, "volume": .01, "profit": 4, "swap": 0, "fee": 0, "commission": -.25},
             {"entry": 1, "volume": .01, "profit": 1, "swap": 0, "fee": 0, "commission": -.25}]
    original = trade()
    capture(store, [signal()], [original], [], [snapshot()], "demo", 100, {123: deals})
    label = store.labels("demo")[0]["payload"]
    assert label["costs_complete"] is True
    assert label["r"] == .175
    assert label["ledger_net"] == 5
    assert original["net_pnl"] == 5


def test_incomplete_position_history_never_qualifies_as_measured_net(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    deals = [{"entry": 0, "volume": .02, "profit": 0, "swap": 0, "fee": 0, "commission": -1},
             {"entry": 1, "volume": .01, "profit": 4, "swap": 0, "fee": 0, "commission": -.25}]
    capture(store, [signal()], [trade()], [], [snapshot()], "demo", 100, {123: deals})
    assert store.labels("demo")[0]["payload"]["costs_complete"] is False


def test_netting_history_larger_than_trade_lot_is_not_a_measured_label(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    deals = [{"entry": 0, "volume": .04, "profit": 0, "swap": 0, "fee": 0, "commission": -1},
             {"entry": 1, "volume": .04, "profit": 10, "swap": 0, "fee": 0, "commission": -1}]
    capture(store, [signal()], [{**trade(), "lot_size": .02}], [], [snapshot()], "demo", 100, {123: deals})
    assert store.labels("demo")[0]["payload"]["costs_complete"] is False
