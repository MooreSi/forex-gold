"""Shapes the Chart tab reads.

Field names are the bridge's own (`ts`, `open`, `high`, `low`, `close`), not
renamed on the way through. Renaming here would mean the browser, the API and
the bridge each know the candle by a different name, and a mismatch would
surface as a blank chart rather than an error.
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, model_validator


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


# Short name -> the column `get_open_trades` really produces. Module level
# rather than a class attribute: a leading underscore inside a pydantic model
# makes it a private attribute, not a dict.
_TRADE_COLUMN_FOR = {
    "id": "trade_id", "entry": "entry_price", "lots": "lot_size",
    "sl": "stop_loss", "tp": "tp1",
    # An OPEN position's P&L is the broker's running number, not a realised
    # one -- `net_pnl` is only filled once it closes.
    "pnl": "mt5_profit",
}


class ChartTrade(BaseModel):
    """An open position as the chart draws it. `extra="allow"` because the
    trades panel shows fields the chart overlay does not, and this layer is not
    the place to decide which of the engine's fields the UI is allowed to see.

    **The short names are filled from the real columns, not renamed.** The
    engine hands back `vantage_simulated_trades` rows verbatim -- `trade_id`,
    `entry_price`, `lot_size`, `stop_loss`, `tp1` -- and every short field
    declared here came back null until 2026-09-21, so the positions table
    showed an em dash in every column but Side, and `CandleChart` drew no
    entry marker at all because it filters on `typeof t.entry === "number"`.

    Filled here rather than renamed in the engine: `get_open_trades` feeds
    several screens that already read the long column names. They are COPIED,
    never moved, for the same reason -- a validation alias would consume the
    original and silently break every one of those screens to fix this one.
    """
    model_config = {"extra": "allow"}

    # `trade_id` is a uuid-ish string; some callers still send an int id.
    id: Optional[Union[int, str]] = None
    direction: Optional[str] = None
    entry: Optional[float] = None
    lots: Optional[float] = None
    sl: Optional[float] = None
    # A trade has up to eight take-profits and this carries one: the first,
    # which is what the panel shows. The rest travel under their own names.
    tp: Optional[float] = None
    pnl: Optional[float] = None

    @model_validator(mode="before")
    @classmethod
    def _fill_short_names(cls, data):
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for short, column in _TRADE_COLUMN_FOR.items():
            # Only when the short name is genuinely absent or null: a caller
            # that already speaks the short names is answering for itself.
            if out.get(short) is None and out.get(column) is not None:
                out[short] = out[column]
        return out
