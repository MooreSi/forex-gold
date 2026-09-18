"""Backtest tab — walk recorded signals against historical candles or ticks.

Places nothing. Every number here is a simulation over data the broker has
already published, and the only writes are to the caller's screen.

The one rule worth stating: a strategy the walk could not simulate comes back
carrying its reason, never as zeros. `template_simulator.unsupported_reason`
exists because a refused template reaching the comparison table as
"0 trades, 0 loss" sits beside a row showing a real drawdown and reads as an
argument for the strategy that was never tested.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.api.schemas.backtest import (
    BacktestOptions, BacktestRequest, BacktestResult,
)
from backend.src.controllers import backtest_controller as bt_ctl
from backend.src.controllers import broker_controller as broker_ctl
from backend.src.controllers import trading_controller as trading_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# The timeframes the page offered, in the bridge's own names.
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
GRANULARITIES = ["candles", "ticks"]

# The tick walk asks the bridge for a TIME RANGE, not a bar count, and the
# bridge bounds it to one day per request. Asking for a month of ticks is tens
# of megabytes the bridge will refuse, so the window is clamped here and the
# response says what was actually walked.
TICKS_MAX_DAYS = 1

# Bars per day, used to turn "30 days of M5" into a candle count the bridge
# understands. Approximate by design: the market is closed at weekends, so
# over-asking and letting the bridge return what exists is the safe direction.
_BARS_PER_DAY = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48,
                 "H1": 24, "H4": 6, "D1": 1}


def _as_dict(value: Any) -> dict:
    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else dict(value)


@router.get("/options", response_model=BacktestOptions)
async def options() -> dict:
    """What the form can offer: built-in strategies, and which EA templates are
    backtestable at all."""
    return {
        "strategies": trading_ctl.build_strategy_catalogue(),
        "templates": bt_ctl.summarise_templates(broker_ctl.list_ea_templates()),
        "timeframes": TIMEFRAMES,
        "granularities": GRANULARITIES,
        "min_trades_per_side": bt_ctl.MIN_TRADES_PER_SIDE,
        "broker_tz_offset": bt_ctl.BROKER_TZ_OFFSET,
    }


@router.post("/run", response_model=BacktestResult)
async def run(body: BacktestRequest, eng: Any = Depends(engine_dep)) -> dict:
    if body.timeframe not in TIMEFRAMES:
        raise Refusal(f"Unknown timeframe {body.timeframe!r}.", status_code=400)
    if body.granularity not in GRANULARITIES:
        raise Refusal(f"Unknown granularity {body.granularity!r}.", status_code=400)

    signals = bt_ctl.signals_from_db(body.live_trades_only)
    if not signals:
        return {
            "results": [], "filtered": {}, "signals_loaded": 0, "candles_loaded": 0,
            "granularity": body.granularity,
            "note": ("No recorded signals to walk. Signals arrive from the "
                     "Telegram channels and the engines; a fresh install has none."),
        }

    count = _BARS_PER_DAY.get(body.timeframe, 288) * body.days
    candles = await eng.get_candles_for_symbol("XAUUSD", body.timeframe, count)
    if not candles:
        raise Refusal(
            "The bridge returned no candles for that window. Check the MT5 "
            "bridge is connected before running a backtest.",
        )

    # Filtering is always against candles: it decides which signals fall inside
    # the window at all, and a tick range cannot answer that for a month.
    kept, stats = bt_ctl.filter_signals(signals, candles, body.max_sl_pts)

    if body.granularity == "ticks":
        # The tick walk gets TICKS. Handing it candles produces a full set of
        # numbers computed from the wrong data — which is the worst kind of
        # wrong here, because nothing about the result looks unusual.
        to_ts = time.time()
        from_ts = to_ts - TICKS_MAX_DAYS * 86_400
        ticks = await eng.get_ticks_range(from_ts, to_ts)
        if not ticks:
            raise Refusal(
                "The bridge returned no ticks. Tick history is bounded to one "
                "day per request and needs the MT5 bridge connected.",
            )
        results = bt_ctl.run_backtest_ticks(
            kept, ticks, body.strategies,
            starting_balance=body.starting_balance,
            spread_pts=body.spread_pts,
            commission_per_lot=body.commission_per_lot,
        )
        return {
            "results": [_as_dict(v) for v in results.values()],
            "filtered": _as_dict(stats),
            "signals_loaded": len(signals),
            "candles_loaded": len(ticks),
            "granularity": body.granularity,
            "note": (f"Walked {len(ticks):,} ticks over the last "
                     f"{TICKS_MAX_DAYS} day — the bridge bounds tick history "
                     "to one day per request."),
        }

    results = bt_ctl.run_backtest(
        kept, candles, body.strategies,
        starting_balance=body.starting_balance,
        risk_pct=body.risk_pct,
        spread_pts=body.spread_pts,
        lots_per_trade=body.lots_per_trade,
        commission_per_lot=body.commission_per_lot,
        split_fraction=body.split_fraction,
    )

    return {
        "results": [_as_dict(v) for v in results.values()],
        "filtered": _as_dict(stats),
        "signals_loaded": len(signals),
        "candles_loaded": len(candles),
        "granularity": body.granularity,
    }
