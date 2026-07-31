"""Telegram signal history reads -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.get_tg_signals, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-tg-signals-migration/020-*.md.

Takes `tg_reader` as an explicit optional parameter (anything exposing
get_group_name(group_id: str) -> Optional[str], matching TelegramReader's
real shape) instead of reading self._tg_reader.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.db import database as db_module


def get_tg_signals(limit: int = 50, tg_reader: Optional[Any] = None) -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            # Show all signals — historical/instant_historical are displayed
            # with a grey badge so the user can see what was received during
            # a restart backfill even if it was too old to execute.
            # instant_historical records (bare "Buy Now" messages) are excluded
            # as they are low-value noise.
            "SELECT * FROM vantage_tg_signals "
            "WHERE status != 'instant_historical' "
            "ORDER BY parsed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = [db_module.row_to_dict(r) for r in rows]
    # Resolve missing group names from TG reader
    for r in result:
        if not r.get("group_name") and r.get("group_id") and tg_reader:
            name = tg_reader.get_group_name(str(r["group_id"]))
            if name:
                r["group_name"] = name
    return result
