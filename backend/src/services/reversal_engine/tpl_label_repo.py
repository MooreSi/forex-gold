"""SQL for the template label. docs/todo/reversal-engine/240.

Read and written by `tpl_label` (the sweep) and read by `edge_model`
through `get_ml_training_data`, which selects every column.
"""
from __future__ import annotations

from typing import Iterable

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


def store_tpl(pairs: Iterable[tuple[int, float]]) -> None:
    for sid, r in pairs:
        get_db().run("UPDATE re_signals SET tpl_r=? WHERE id=?", r, sid)


def count_labelled() -> int:
    row = get_db().get("SELECT COUNT(*) FROM re_signals WHERE tpl_r IS NOT NULL")
    return int(row[0]) if row else 0
