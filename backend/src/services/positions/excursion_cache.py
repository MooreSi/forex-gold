"""Excursion cache for the Analysis table (docs/todo/024).

Both calls run from a request handler on the event loop, so both dispatch off
it here, the same shape as `spread_cache`.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.positions import excursion_cache_repo as _repo

__all__ = ["get_cached", "cache"]


async def get_cached(tickets: list) -> dict:
    return await to_db_thread(_repo.get_cached_excursions, tickets)


async def cache(ticket, mae, mfe, resolution) -> None:
    return await to_db_thread(_repo.cache_excursion, ticket, mae, mfe, resolution)
