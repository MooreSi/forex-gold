"""SQL for each Breakout signal's market state. docs/todo/017 item 4.

Written only by `market_state_sweep`; read by the regime report
(`tools/regime_table.py`). Nothing on the order path reads it.
"""
from __future__ import annotations

from typing import Iterable

from backend.src.services.breakout_signal.breakout_signal_repo import get_db


def signals_missing_state() -> list[dict]:
    """Every signal not yet measured, newest first."""
    rows = get_db().all(
        "SELECT id, created_at, direction FROM bo_signals "
        "WHERE market_state_json IS NULL AND created_at IS NOT NULL "
        "ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def store_state(pairs: Iterable[tuple[int, str]]) -> None:
    for sid, payload in pairs:
        get_db().run("UPDATE bo_signals SET market_state_json=? WHERE id=?", payload, sid)
