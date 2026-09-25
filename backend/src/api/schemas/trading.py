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

from pydantic import BaseModel, Field, model_validator

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


# Short name -> the `vantage_signals` column it is filled from. Module level
# rather than a class attribute, for the same reason as `_TRADE_COLUMN_FOR` in
# schemas/chart.py: a leading underscore inside a pydantic model makes it a
# private attribute, not a dict.
_SIGNAL_COLUMN_FOR = {
    "id": "signal_id", "source": "source_name", "entry": "entry_low",
    "sl": "stop_loss", "tp": "tp1", "lots": "lot_size",
}


class SignalOut(BaseModel):
    """A signal as the Signals table draws it.

    **The short names are filled from the real columns, not renamed.** The rows
    are `vantage_signals` verbatim and the browser was reading `source` and
    `entry` -- neither of which is a column -- so every cell but Side and
    Status rendered an em dash and the table was, in the owner's words, in need
    of "populating with details" (2026-09-21). Exactly the shape of the
    Positions table's bug the same day, and fixed the same way.

    They are COPIED, never moved. `get_signals` also feeds the signal editor
    and the AI commentary panel, which read the long names; a validation alias
    would consume the original and break both to fix this one.

    `entry` is the LOW end of the band, and `entry_high` travels under its own
    name, because a signal quotes a range and a single number implies a
    precision it does not have.

    `extra="allow"` for the same reason as `ChartTrade`: this layer is not the
    place to decide which of a signal's twenty columns the UI may see.
    """
    model_config = {"extra": "allow"}

    id: Optional[str] = None
    source: Optional[str] = None
    direction: Optional[str] = None
    entry: Optional[float] = None
    entry_high: Optional[float] = None
    sl: Optional[float] = None
    # One of up to eight. The rest travel under their own names.
    tp: Optional[float] = None
    lots: Optional[float] = None
    status: Optional[str] = None
    # What HAPPENED, as opposed to how the signal ended. "closed" is the same
    # word for a signal that took 300 dollars and one that gave back 300, so
    # the screens were showing a list of finished things with no way to tell
    # which had worked. Decided by services/signals/outcomes.py from the
    # trades the signal produced -- never in the browser, which would be a
    # second answer to "did this win". Null for a signal that never traded or
    # is still running; "won", "lost" or "flat" otherwise.
    outcome: Optional[str] = None
    net_pnl: Optional[float] = None

    @model_validator(mode="before")
    @classmethod
    def _fill_short_names(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for short, column in _SIGNAL_COLUMN_FOR.items():
            # Only when the short name is genuinely absent or null: a caller
            # that already speaks the short names is answering for itself.
            if out.get(short) is None and out.get(column) is not None:
                out[short] = out[column]
        return out
