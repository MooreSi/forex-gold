"""Trade excursion cache: MAE/MFE per closed position (docs/todo/024).

The writer for `trade_excursion_cache`. Like `trade_spread_cache` it is keyed
by MT5 position id, because the Analysis table is built from MT5's deal
history rather than any one engine's trade table, and it is computed once:
a past price path never changes.
"""
from __future__ import annotations

import logging
import time

from backend.src.db.database import db

log = logging.getLogger(__name__)

__all__ = ["get_cached_excursions", "cache_excursion"]


def get_cached_excursions(position_ids: list[int]) -> dict[int, dict]:
    """{position_id: {mae, mfe, res}} for positions already measured.

    `{}` on any error: a missing excursion is a blank cell, not a lost table.
    """
    if not position_ids:
        return {}
    try:
        with db() as conn:
            placeholders = ",".join("?" * len(position_ids))
            rows = conn.execute(
                f"SELECT position_id, mae_price, mfe_price, resolution "
                f"FROM trade_excursion_cache WHERE position_id IN ({placeholders})",
                list(position_ids),
            ).fetchall()
        return {r[0]: {"mae": r[1], "mfe": r[2], "res": r[3]} for r in rows}
    except Exception:
        return {}


def cache_excursion(position_id: int, mae, mfe, resolution: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trade_excursion_cache "
                "(position_id, mae_price, mfe_price, resolution, computed_at) "
                "VALUES (?,?,?,?,?)",
                (position_id, mae, mfe, resolution, time.time()),
            )
    except Exception as e:
        log.debug("cache_excursion error: %s", e)
