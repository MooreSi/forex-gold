"""SQL for the cross-asset context. docs/todo/reversal-engine/230.

Read and written only by `xasset_sweep`, `meta_label_schedule` and the
panel. Nothing on the order path reads these, except that the live row
carries `xasset_json` because `get_open_signals` selects every column.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def signals_missing_xasset() -> list[dict]:
    """Every signal not yet measured, newest first. Unfilled is NULL; a
    measured row always holds a JSON object, even when every peer was
    missing, so this cannot re-select a row the sweep already wrote."""
    rows = get_db().all(
        "SELECT id, created_at, direction FROM re_signals "
        "WHERE xasset_json IS NULL AND created_at IS NOT NULL "
        "ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


def store_xasset(pairs: Iterable[tuple[int, str]]) -> None:
    for sid, payload in pairs:
        get_db().run("UPDATE re_signals SET xasset_json=? WHERE id=?", payload, sid)


def stored_records() -> list[dict]:
    rows = get_db().all(
        "SELECT created_at, xasset_json FROM re_signals "
        "WHERE xasset_json IS NOT NULL ORDER BY created_at"
    )
    return [dict(r) for r in rows]


def record_fit(ts: float, n: int, auc_base: Optional[float],
               auc_xasset: Optional[float], installed: str, per_peer: dict) -> None:
    get_db().run(
        "INSERT INTO re_xasset_fits (ts, n, auc_base, auc_xasset, installed, per_peer_json) "
        "VALUES (?,?,?,?,?,?)",
        ts, n, auc_base, auc_xasset, installed, json.dumps(per_peer))


def fit_history(limit: int = 365) -> list[dict]:
    """The most recent `limit` fits, oldest first."""
    rows = get_db().all(
        "SELECT ts, n, auc_base, auc_xasset, installed, per_peer_json "
        "FROM re_xasset_fits ORDER BY ts DESC LIMIT ?", limit)
    out = []
    for r in reversed([dict(x) for x in rows]):
        try:
            r["per_peer"] = json.loads(r.pop("per_peer_json") or "{}")
        except (TypeError, ValueError):
            r["per_peer"] = {}
        out.append(r)
    return out
