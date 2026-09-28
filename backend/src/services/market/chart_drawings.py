"""Drawings on the Broker chart (docs/todo/011): trend lines, levels,
rectangles and Fibonacci retracements the owner draws for planning.

Pictures only. Nothing that trades, signals or gates reads them. The shape of
a drawing (which tools exist, how many points each takes) is checked at the
API; this holds the one rule that needs the store: a cap per symbol.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.market import chart_drawings_repo as repo

# Every drawing is re-read on load and redrawn on every pan and zoom; a cap
# keeps a runaway client from making the chart unusable.
MAX_PER_SYMBOL = 500


class TooManyDrawings(Exception):
    pass


def list_for(symbol: str) -> list[dict]:
    return repo.list_for(symbol)


def create(symbol: str, kind: str, points: list[dict]) -> dict:
    if repo.count_for(symbol) >= MAX_PER_SYMBOL:
        raise TooManyDrawings(
            f"{symbol} already has {MAX_PER_SYMBOL} drawings. Delete some first.")
    return repo.insert(symbol, kind, points)


def update_points(drawing_id: int, points: list[dict]) -> Optional[dict]:
    return repo.update_points(drawing_id, points)


def delete(drawing_id: int) -> bool:
    return repo.delete(drawing_id)


async def list_for_async(symbol: str) -> list[dict]:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(list_for, symbol)


async def create_async(symbol: str, kind: str, points: list[dict]) -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(create, symbol, kind, points)


async def update_points_async(drawing_id: int, points: list[dict]) -> Optional[dict]:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(update_points, drawing_id, points)


async def delete_async(drawing_id: int) -> bool:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(delete, drawing_id)
