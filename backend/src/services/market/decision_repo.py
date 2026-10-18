"""Append-only decision observations in each engine's own database."""
import json

_SCHEMA = """CREATE TABLE IF NOT EXISTS decision_observations (
    id INTEGER PRIMARY KEY, signal_ref TEXT NOT NULL, engine TEXT NOT NULL,
    stage TEXT NOT NULL, observed_at REAL NOT NULL, policy_hash TEXT NOT NULL,
    payload TEXT NOT NULL)"""


def append(db, signal_ref, observation):
    payload = json.dumps(observation, sort_keys=True, allow_nan=False)
    with db.transaction():
        db.run(_SCHEMA)
        db.run("INSERT INTO decision_observations "
               "(signal_ref,engine,stage,observed_at,policy_hash,payload) VALUES (?,?,?,?,?,?)",
               signal_ref, observation["engine"], observation["stage"],
               observation["observed_at"], observation["policy_hash"], payload)


def for_signal(db, signal_ref):
    db.run(_SCHEMA)
    return [json.loads(r[0]) for r in db.all(
        "SELECT payload FROM decision_observations WHERE signal_ref=? ORDER BY id", signal_ref)]
