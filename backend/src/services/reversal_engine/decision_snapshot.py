"""Record exact scoring inputs and policy identity without affecting decisions.

All eligible candidates are retained, including alternatives the ranker did
not select. These are observations, not invented outcomes or broker fills.
Fill observations are append-only so retry attempts remain distinguishable.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import time

from backend.src.services.reversal_engine import decision_snapshot_repo as repo
from backend.src.services.reversal_engine import ml_engine as ml

log = logging.getLogger("reversal_engine")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _valid_features(features):
    try:
        return (isinstance(features, (list, tuple)) and len(features) == len(ml.FEATURE_NAMES)
                and all(math.isfinite(float(v)) for v in features))
    except (TypeError, ValueError):
        return False


def _policy(strategy):
    from backend.src.services.broker import ea_templates as et
    policy = {"strategy": strategy, "template": None}
    if et.is_template_override(strategy or ""):
        policy["template"] = et.get_ea_template(et.template_name_from_override(strategy))
        policy["status"] = "observed" if policy["template"] else "unavailable"
    return policy


def _record(signal_ref, stage, rank, chosen, context, features, predicted_r, ts, policy):
    try:
        valid = _valid_features(features)
        score = float(predicted_r) if predicted_r is not None else None
        score = score if score is not None and math.isfinite(score) else None
        state = "ok" if valid and score is not None else "no_prediction" if valid else "invalid_features"
        model = {"version": ml._version, "training": ml._train_history[-1:] or None,
                 "online_count": ml._labeled_count}
        repo.insert({"signal_ref": signal_ref, "stage": stage, "candidate_rank": rank,
                     "chosen": int(chosen), "decision_ts": ts,
                     "schema_hash": _hash(ml.FEATURE_NAMES), "model_id": _hash(model),
                     "policy_hash": _hash(policy), "policy_json": _json(policy),
                     "features_json": _json(list(features)) if valid else None,
                     "context_json": _json({"signal": context, "feature_names": ml.FEATURE_NAMES,
                                            "model": model, "source_availability": "not measured"}),
                     "predicted_r": score, "health": state})
    except Exception as exc:
        log.warning("[RE-Decision] could not record %s %s: %s", signal_ref, stage, exc)


def record_candidates(selected: dict, eligible: list) -> None:
    try:
        policy = _policy(selected.get("strategy"))
        for rank, (_, _, context, features, score) in enumerate(eligible):
            _record(selected["signal_ref"], "creation", rank,
                    context.get("signal_ref") == selected["signal_ref"],
                    context, features, score, float(context["created_at"]), policy)
    except Exception as exc:
        log.warning("[RE-Decision] candidate snapshot failed: %s", exc)


def record_fill(signal: dict, features, predicted_r, decision_ts=None) -> None:
    try:
        _record(signal["signal_ref"], "fill", 0, True, signal, features,
                predicted_r, time.time() if decision_ts is None else decision_ts,
                _policy(signal.get("strategy")))
    except Exception as exc:
        log.warning("[RE-Decision] fill snapshot failed: %s", exc)


def snapshots(signal_ref: str) -> list[dict]:
    return repo.snapshots(signal_ref)
