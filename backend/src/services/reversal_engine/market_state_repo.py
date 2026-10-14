"""SQL for each Reversal signal's market state. docs/todo/017 item 4.

Written only by `market_state_sweep`; read by the regime report
(`tools/regime_table.py`). Nothing on the order path reads it, except that a
live row carries the column because `get_open_signals` selects every column.
"""
from __future__ import annotations

from typing import Iterable

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def signals_missing_state() -> list[dict]:
    """Every signal not yet measured, newest first. A measured row always
    holds a JSON object, so this cannot re-select a row already written."""
    rows = get_db().all(
        "SELECT id, created_at, direction FROM re_signals "
        "WHERE market_state_json IS NULL AND created_at IS NOT NULL "
        "ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def store_state(pairs: Iterable[tuple[int, str]]) -> None:
    for sid, payload in pairs:
        get_db().run("UPDATE re_signals SET market_state_json=? WHERE id=?", payload, sid)
