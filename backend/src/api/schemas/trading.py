"""Shapes the Trading tab reads and writes.

The order request models are the money-touching part of this layer, so they are
deliberately dumb: they name fields and types and validate nothing about
whether an order is *allowed*. The backend decides that. A model here that
rejected, say, a lot size above some number would be a second risk check that
drifts from the real one, which is the failure the frontend conventions name
outright ("Duplicating a risk check in the UI produces two answers that drift").
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Direction = Literal["BUY", "SELL"]


class MarketOrderRequest(BaseModel):
    """Mirrors `TradingRuntime.open_manual_market_order`'s signature exactly,
    defaults included. `stop_loss=None` means "let DPM compute an ATR stop" and
    `lot_size=None` means "size it from the risk settings" — both behaviours of
    the engine, neither re-implemented here."""
    direction: Direction
    stop_loss: Optional[float] = None
    lot_size: Optional[float] = None
    strategy: Optional[str] = None
    take_profit: Optional[float] = None
    source_name: str = "manual_market"


class LimitOrderRequest(BaseModel):
    """Mirrors `TradingRuntime.open_manual_limit_order`. TP1–TP8 are separate
    named fields rather than a list because that is the engine's signature, and
    a list would have to be unpacked back into it by this layer."""
    direction: Direction
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    tp5: Optional[float] = None
    tp6: Optional[float] = None
    tp7: Optional[float] = None
    tp8: Optional[float] = None
    lot_size: Optional[float] = None
    notes: str = ""


class CloseRequest(BaseModel):
    reason: str = "manual_close"


class PartialCloseRequest(BaseModel):
    lots_to_close: float = Field(gt=0)
    close_price: float
    reason: str = "TP"


class OpenFromSignalRequest(BaseModel):
    lot_size_override: Optional[float] = None
    age_lot_mult: float = 1.0


class HaltState(BaseModel):
    """Why the Execute button is disabled, if it is.

    Rendered as text next to the control. A greyed button with no explanation
    is indistinguishable from a broken one — frontend conventions §8.
    """
    reason: str
    market_closed: bool
    circuit_breaker: dict


class RiskSettingsUpdate(BaseModel):
    model_config = {"extra": "allow"}


class ChannelStrategyOverride(BaseModel):
    source: str
    strategy: Optional[str] = None
    auto: bool = True


class PauseWrite(BaseModel):
    """Halt new orders for a number of hours, or until a given moment.

    Both optional and `until` wins: a dialog that offers "pause for N hours"
    and "pause until HH:MM" has to send whichever the operator filled in, and
    an empty hours field must not silently become 0 -- which would be a pause
    already in the past.
    """

    hours: float | None = None
    until: float | None = None
