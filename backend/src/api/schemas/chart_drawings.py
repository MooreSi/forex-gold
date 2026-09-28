"""Shapes for the Broker chart's drawings (docs/todo/011).

The tool list and how many points each takes are checked here, before
anything is stored: a trend line with one point is a drawing nobody can
render, and it would come back on every page load.
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DrawingKind = Literal["trend", "hline", "rect", "fib", "position"]

# A horizontal level is one price; a position is entry, stop and target, in
# that order; everything else is two corners.
POINTS_PER_KIND: dict[str, int] = {
    "trend": 2, "hline": 1, "rect": 2, "fib": 2, "position": 3,
}

SYMBOL_PATTERN = r"^[A-Za-z0-9._#-]{1,32}$"


class Point(BaseModel):
    time: float
    price: float

    @field_validator("time", "price")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class DrawingIn(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    kind: DrawingKind
    points: list[Point]

    @model_validator(mode="after")
    def _points_fit_the_tool(self) -> "DrawingIn":
        want = POINTS_PER_KIND[self.kind]
        if len(self.points) != want:
            raise ValueError(f"a {self.kind} takes {want} point(s), got {len(self.points)}")
        return self


class DrawingMove(BaseModel):
    points: list[Point] = Field(min_length=1, max_length=3)


class DrawingOut(BaseModel):
    id: int
    symbol: str
    kind: str
    points: list[Point]
    created_at: float
    updated_at: float
