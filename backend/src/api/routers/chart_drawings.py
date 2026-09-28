"""Drawings on the Broker chart (docs/todo/011): trend lines, levels,
rectangles, Fibonacci. Pictures for planning; nothing that trades reads them,
and no route here reaches the broker."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.src.api.errors import Refusal
from backend.src.api.schemas.chart_drawings import (
    SYMBOL_PATTERN, DrawingIn, DrawingMove, DrawingOut,
)
from backend.src.controllers import chart_drawings_controller as drawings_ctl

router = APIRouter(prefix="/api/chart/drawings", tags=["chart"])


@router.get("", response_model=list[DrawingOut])
async def list_drawings(symbol: str = Query("XAUUSD", pattern=SYMBOL_PATTERN)) -> list[dict]:
    return await drawings_ctl.list_for(symbol)


@router.post("", response_model=DrawingOut)
async def create_drawing(body: DrawingIn) -> dict:
    try:
        return await drawings_ctl.create(
            body.symbol, body.kind, [p.model_dump() for p in body.points])
    except drawings_ctl.TooManyDrawings as exc:
        raise Refusal(str(exc), status_code=409) from None


@router.put("/{drawing_id}", response_model=DrawingOut)
async def move_drawing(drawing_id: int, body: DrawingMove) -> dict:
    out = await drawings_ctl.update_points(drawing_id, [p.model_dump() for p in body.points])
    if out is None:
        raise Refusal(f"No drawing {drawing_id}.", status_code=404)
    return out


@router.delete("/{drawing_id}")
async def delete_drawing(drawing_id: int) -> dict:
    if not await drawings_ctl.delete(drawing_id):
        raise Refusal(f"No drawing {drawing_id}.", status_code=404)
    return {"deleted": drawing_id}
