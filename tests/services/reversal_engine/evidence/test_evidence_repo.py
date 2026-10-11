"""Temporary evidence databases, no broker calls."""
from contextlib import contextmanager
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


CONTEXT_KINDS = ("calendar", "calendar_batch", "cme_book", "futures_bar", "broker_tick")


def context_reference(rows, env, decision_ts):
    """Latest arrival (then highest id) per source/kind/key, the rule features.context relies on."""
    latest = {}
    for row in rows:
        if row["env"] != env or row["kind"] not in CONTEXT_KINDS or row["available_at"] > decision_ts:
            continue
        group = (row["source"], row["kind"], row["key"])
        if group not in latest or (row["available_at"], row["id"]) > (latest[group]["available_at"], latest[group]["id"]):
            latest[group] = row
    return sorted(r["id"] for r in latest.values())


def test_context_events_is_the_latest_arrival_per_source_kind_and_key(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    events = []
    for n in range(40):
        events.append({"source": "mt5", "kind": "broker_tick", "key": "XAUUSD", "event_ts": n,
                       "available_at": n, "payload": {"spread": n}})
        events.append({"source": "ff" if n % 2 else "te", "kind": "calendar", "key": f"E{n % 5}",
                       "event_ts": 100, "available_at": n // 3, "payload": {"rev": n}})
        events.append({"source": "mt5_ledger", "kind": "broker_label", "key": "T1", "event_ts": n,
                       "available_at": n, "payload": {"n": n}})
    events.append({"source": "mt5", "kind": "broker_tick", "key": "XAUUSD", "event_ts": 41,
                   "available_at": 20, "payload": {"spread": "tie, later id"}})
    store.put_many("demo", events)
    store.put_many("live", [{**e, "available_at": 0} for e in events[:30]])
    with store.connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM observations")]
    for decision_ts in (-1, 0, 5, 13, 20, 20.5, 39, 1000):
        got = sorted(r["id"] for r in store.context_events("demo", decision_ts))
        assert got == context_reference(rows, "demo", decision_ts), decision_ts
    tie = [r for r in store.context_events("demo", 20) if r["kind"] == "broker_tick"]
    assert [r["payload"]["spread"] for r in tie] == ["tie, later id"]


class CountingStore(EvidenceStore):
    """Counts SQLite VM steps so the test sees work done, not wall time."""
    steps = 0

    @contextmanager
    def connect(self):
        with super().connect() as db:
            db.set_progress_handler(self._count, 1)
            yield db

    def _count(self):
        CountingStore.steps += 1
        return 0


def context_steps(tmp_path, ticks):
    store = CountingStore(tmp_path / f"e{ticks}.db")
    store.put_many("demo", [{"source": "mt5", "kind": "broker_tick", "key": "XAUUSD", "event_ts": n,
                             "available_at": n, "payload": {"spread": 1}} for n in range(ticks)])
    CountingStore.steps = 0
    store.context_events("demo", ticks / 2)
    return CountingStore.steps


def test_context_lookup_cost_does_not_grow_with_tick_history(tmp_path):
    # broker_ledger calls this once per closed trade every minute; on
    # 2026-10-11 a 644k-tick history made each call 1.1s and pinned a core.
    small, large = context_steps(tmp_path, 500), context_steps(tmp_path, 20000)
    assert small > 0
    assert large < small * 2, (small, large)
