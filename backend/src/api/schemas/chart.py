"""Shapes the Chart tab reads.

Field names are the bridge's own (`ts`, `open`, `high`, `low`, `close`), not
renamed on the way through. Renaming here would mean the browser, the API and
the bridge each know the candle by a different name, and a mismatch would
surface as a blank chart rather than an error.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class Candle(BaseModel):
    ts: float
    open: float
    high: float
    low: float
    close: float


class TickOut(BaseModel):
    bid: float
    ask: float
    mid: float
    spread: float
    spread_points: float
    timestamp: float
    source: str


class FvgZone(BaseModel):
    """A fair-value gap, already thinned to what is worth drawing by
    `chart_controller.select_display_fvgs`. The API does not decide which gaps
    matter."""
    ts: float
    top: float
    bottom: float
    direction: str


class Overlays(BaseModel):
    """EMA, RSI and FVG series for one candle window.

    One endpoint, not three. The overlays are drawn on the same candles, and
    three endpoints means three fetches that can disagree about the window —
    which reads as an EMA that floats off the price rather than as an error.
    """
    timeframe: str
    count: int
    emas: dict[str, list[Optional[float]]]
    rsi: list[Optional[float]]
    fvgs: list[FvgZone]


class ChartTrade(BaseModel):
    """An open position as the chart draws it. `extra="allow"` because the
    trades panel shows fields the chart overlay does not, and this layer is not
    the place to decide which of the engine's fields the UI is allowed to see.
    """
    model_config = {"extra": "allow"}

    id: Optional[int] = None
    direction: Optional[str] = None
    entry: Optional[float] = None
    lots: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
