"""Append-only scoring observations, separate from mutable signal outcomes."""
from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def insert(snapshot: dict) -> None:
    get_db().run(
        "INSERT INTO re_decision_snapshots (signal_ref,stage,candidate_rank,chosen,"
        "decision_ts,schema_hash,model_id,policy_hash,policy_json,features_json,"
        "context_json,predicted_r,health) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        snapshot["signal_ref"], snapshot["stage"], snapshot["candidate_rank"],
        snapshot["chosen"], snapshot["decision_ts"], snapshot["schema_hash"],
        snapshot["model_id"], snapshot["policy_hash"], snapshot["policy_json"],
        snapshot["features_json"], snapshot["context_json"], snapshot["predicted_r"],
        snapshot["health"])


def snapshots(signal_ref: str) -> list[dict]:
    return [dict(r) for r in get_db().all(
        "SELECT * FROM re_decision_snapshots WHERE signal_ref=? ORDER BY id", signal_ref)]
