"""SQL for the template label. docs/todo/reversal-engine/240.

Read and written by `tpl_label` (the sweep) and read by `edge_model`
through `get_ml_training_data`, which selects every column.
"""
from __future__ import annotations

from typing import Iterable
import hashlib
import json
import math

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def signals_missing_tpl(triggered_before: float) -> list[dict]:
    """Every signal that triggered before `triggered_before` and has no
    label yet, newest first. A signal that never triggered placed no trade
    and has nothing to label."""
    rows = get_db().all(
        "SELECT id, direction, trigger_price, trigger_time FROM re_signals "
        "WHERE tpl_r IS NULL AND trigger_time IS NOT NULL AND trigger_price > 0 "
        "AND trigger_time < ? ORDER BY trigger_time DESC",
        triggered_before,
    )
    return [dict(r) for r in rows]


def store_tpl(pairs: Iterable[tuple[int, float]], *, contract: dict | None = None,
              available_at: float | None = None) -> None:
    """Write label and its actual research availability atomically.

    Legacy callers remain unlabeled in provenance; no guessed historical
    template definition or label-production timestamp is written for them.
    """
    policy_json = json.dumps(contract["policy"], sort_keys=True, allow_nan=False) if contract else None
    policy_hash = hashlib.sha256(policy_json.encode()).hexdigest() if policy_json else None
    if contract and (available_at is None or not math.isfinite(float(available_at))):
        raise ValueError("label availability must be measured")
    with get_db().transaction():
        for sid, r in pairs:
            if not math.isfinite(float(r)):
                raise ValueError("label must be finite")
            get_db().run("UPDATE re_signals SET tpl_r=? WHERE id=?", r, sid)
            if contract:
                get_db().run(
                    "INSERT OR REPLACE INTO re_template_label_contracts "
                    "(signal_id,policy_hash,policy_json,cost_pts,available_at,source) VALUES (?,?,?,?,?,?)",
                    sid, policy_hash, policy_json, float(contract["cost_pts"]),
                    float(available_at), contract["source"])


def contract_for(signal_id: int) -> dict | None:
    row = get_db().get("SELECT * FROM re_template_label_contracts WHERE signal_id=?", signal_id)
    return dict(row) if row else None


def count_labelled() -> int:
    row = get_db().get("SELECT COUNT(*) FROM re_signals WHERE tpl_r IS NOT NULL")
    return int(row[0]) if row else 0
