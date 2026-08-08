"""Spread cache for the History page's cost column.

Both reads run from a refresh handler on the event loop, so both dispatch off
it here rather than in the controller.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.positions import spread_cache_repo as _repo

__all__ = ["get_cached", "cache"]


async def get_cached(tickets: list) -> dict:
    return await to_db_thread(_repo.get_cached_spreads, tickets)


async def cache(ticket, price, points, cost) -> None:
    return await to_db_thread(_repo.cache_spread, ticket, price, points, cost)
