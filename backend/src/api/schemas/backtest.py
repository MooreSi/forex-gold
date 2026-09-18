"""Shapes the Backtest tab reads and posts.

`StrategyStats` is a dataclass with an equity curve and a full trade list on
it. Both cross the wire — the results panel draws the curve and the trade list
is what makes a number checkable — but `unsupported_reason` is the field that
matters most and is why these models exist at all: a template the walk refused
must arrive as a refusal, not as zeros. Zeros beside a row showing a real
drawdown read as an argument FOR the strategy that could not be simulated.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class BacktestOptions(BaseModel):
    """Everything the form needs to build itself."""
    strategies: list[dict]
    templates: list[dict]
    timeframes: list[str]
    granularities: list[str]
    min_trades_per_side: int
    broker_tz_offset: float


class BacktestRequest(BaseModel):
    strategies: list[str] = Field(min_length=1)
    timeframe: str = "M5"
    days: int = Field(default=30, ge=1, le=3650)
    granularity: str = "candles"
    live_trades_only: bool = False
    starting_balance: float = 1000.0
    risk_pct: float = 1.0
    lots_per_trade: float = 0.0
    spread_pts: float = 0.4
    commission_per_lot: float = 7.0
    max_sl_pts: float = 50.0
    split_fraction: float = 0.0


class StrategyResult(BaseModel):
    model_config = {"extra": "allow"}

    strategy: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    total_commission: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe: float
    final_balance: float
    equity_curve: list[float] = []
    # Empty when the walk ran. Non-empty when it refused, and then every number
    # above is a zero that must NOT be read as a result.
    unsupported_reason: str = ""


class FilterReport(BaseModel):
    """Why signals were dropped before the walk.

    Shown on the page because a backtest over three signals is not a backtest,
    and without this the only visible symptom is a suspiciously small trade
    count. `candle_start`/`signal_start` are the pair that explains the common
    case: a signal window that does not overlap the candles fetched.
    """
    model_config = {"extra": "allow"}

    total: int = 0
    valid: int = 0
    out_of_window: int = 0
    zero_sl: int = 0
    point_entry: int = 0
    wide_sl: int = 0
    bad_tp: int = 0
    candle_start: Optional[str] = None
    candle_end: Optional[str] = None
    signal_start: Optional[str] = None
    signal_end: Optional[str] = None


class BacktestResult(BaseModel):
    results: list[StrategyResult]
    filtered: FilterReport
    signals_loaded: int
    candles_loaded: int
    granularity: str
    note: Optional[str] = None
