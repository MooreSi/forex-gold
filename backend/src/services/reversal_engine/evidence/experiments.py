"""Reproducible local candidate artifacts; grouped by environment and policy."""
from __future__ import annotations
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from . import candidate, features
from .observations import digest, finite
from backend.src.services.reversal_engine.ml_engine._feature_schema import FEATURE_NAMES


def code_hash():
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(Path(__file__).parent.glob("*.py"))})


def run_candidates(store, env, directory):
    groups = defaultdict(list)
    for event in store.labels(env):
        row = event["payload"]
        base, extra = row.get("features"), row.get("external_features")
        if not row.get("policy_hash") or not base or not extra:
            continue
        values = base + extra
        if len(values) != len(FEATURE_NAMES) + len(features.FEATURE_NAMES) or any(finite(v) is None for v in values):
            continue
        groups[row["policy_hash"]].append({**row, "features": values, "available_at": event["available_at"]})
    known = {r["id"] for r in store.runs()}
    completed = []
    version = code_hash()
    schema = digest(FEATURE_NAMES + features.FEATURE_NAMES)
    for policy, rows in groups.items():
        rows.sort(key=lambda r: r["decision_ts"])
        data_hash = digest(rows)
        identity = digest([env, policy, data_hash, version, schema, candidate.MODEL_PARAMS])
        if identity in known:
            continue
        settled = [r for r in rows if r.get("costs_complete") is True]
        metrics = candidate.evaluate(settled)
        metrics.update(audit_rows=len(rows), missing_cost_rows=len(rows) - len(settled))
        path = Path(directory) / identity
        path.mkdir(parents=True, exist_ok=True)
        model_hash = None
        if len(settled) >= candidate.MIN_ROWS:
            import joblib
            model = candidate.fit(settled)
            temp = path / "candidate.tmp"
            joblib.dump(model, temp)
            model_hash = hashlib.sha256(temp.read_bytes()).hexdigest()
            os.replace(temp, path / "candidate.pkl")
        run = {"id": identity, "created_at": time.time(), "env": env, "policy_hash": policy,
               "dataset_hash": data_hash, "code_hash": version, "schema_hash": schema,
               "model_hash": model_hash, "metrics": metrics,
               "params": {**candidate.MODEL_PARAMS, "label": "broker aggregate net / initial risk",
                          "folds": 4, "embargo_s": 60, "selection_threshold_r": 0,
                          "mode": "shadow; no automatic promotion", "sample": "executed broker trades only"}}
        # Dataset lives in the private data directory, never in the source repo.
        (path / "dataset.json").write_text(json.dumps(rows, sort_keys=True, allow_nan=False))
        (path / "run.json").write_text(json.dumps(run, sort_keys=True, allow_nan=False, indent=2))
        store.experiment(run)  # DB acknowledgement only after artifacts exist
        completed.append(run)
    return completed
