"""Drawings on the Broker chart (docs/todo/011). Forwards to
backend.src.services.market.chart_drawings unchanged. Nothing here trades."""
from __future__ import annotations

from backend.src.services.market import chart_drawings as _drawings

__all__ = ["list_for", "create", "update_points", "delete", "TooManyDrawings"]

TooManyDrawings = _drawings.TooManyDrawings


async def list_for(symbol: str) -> list[dict]:
    return await _drawings.list_for_async(symbol)


async def create(symbol: str, kind: str, points: list[dict]) -> dict:
    return await _drawings.create_async(symbol, kind, points)


async def update_points(drawing_id: int, points: list[dict]) -> dict | None:
    return await _drawings.update_points_async(drawing_id, points)


async def delete(drawing_id: int) -> bool:
    return await _drawings.delete_async(drawing_id)
