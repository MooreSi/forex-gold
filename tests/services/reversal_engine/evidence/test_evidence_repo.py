"""Temporary evidence databases, no broker calls."""
from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore


def event(value=1, seen=20):
    return {"source": "test", "kind": "calendar", "key": "CPI", "event_ts": 10,
            "available_at": seen, "payload": {"value": value}}


def test_revisions_survive_restart_and_same_observation_is_idempotent(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.put("demo", event())
    store.put("demo", event())
    store.put("demo", event(2, 40))
    rows = EvidenceStore(tmp_path / "e.db").events("demo", 100)
    assert [r["payload"]["value"] for r in rows] == [1, 2]
    assert rows[0]["available_at"] == 20


def test_future_arrivals_and_other_accounts_are_excluded(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.put("demo", event())
    store.put("live", event(3))
    store.put("demo", event(2, 40))
    assert [r["payload"]["value"] for r in store.events("demo", 30)] == [1]


def test_provider_health_is_persisted_and_visible(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.health("calendar", "unavailable", "missing credentials", 100)
    assert EvidenceStore(tmp_path / "e.db").status()["calendar"]["state"] == "unavailable"
