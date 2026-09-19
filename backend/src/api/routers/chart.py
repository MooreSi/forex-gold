"""Chart tab — read-only. No endpoint here can move money.

Every handler forwards to `chart_controller` or to a market-data read on the
injected engine, exactly as `frontend/pages/chart/` did. The EMA and RSI maths
is `core_indicators` through the controller, shared with the signal snapshot
capture so the chart and the engine cannot drift about what an EMA 21 is.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from backend.src.api.deps import engine as engine_dep
from backend.src.api.schemas.chart import Candle, ChartTrade, Overlays, TickOut
from backend.src.controllers import chart_controller as chart_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chart", tags=["chart"])

# The selector the page offered, and the MT5 names behind it. Kept here rather
# than in the browser so the API rejects a timeframe the bridge cannot serve
# instead of forwarding it and getting an empty list back.
TIMEFRAMES: dict[str, str] = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1H": "H1", "4H": "H4", "1D": "D1",
}

# EMA periods and the colours the NiceGUI chart used. The colours travel with
# the periods because they are the same decision: gold/orange/sky-blue is how
# the fast, medium and slow averages are told apart, and splitting the pair
# across two files is how they drift.
EMA_PERIODS: list[int] = [9, 21, 50]

# The widest an EMA may be. Not a style limit: the period is a loop bound over
# the candle window, and an unbounded one from a query string is a request the
# browser can make arbitrarily expensive.
MAX_EMA_PERIOD = 1000


def _ema_periods(raw: str | None) -> list[int]:
    """The periods a caller asked for, or the Chart tab's three.

    Set & Forget reads EMA 50 against EMA 200; the Chart tab reads 9/21/50.
    Asking here rather than computing a second EMA in the browser keeps one
    implementation of the maths -- `chart_controller.ema_series` is shared with
    the engine's signal snapshot, and a TypeScript copy would be a second
    answer to what an EMA 200 is.

    A period that is not a usable number is REFUSED rather than dropped.
    Falling back silently would draw an EMA 50 under a legend saying something
    else, which is a line on a chart that is not the line it claims to be.
    """
    from backend.src.api.errors import Refusal
    if not raw:
        return list(EMA_PERIODS)
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            period = int(part)
        except ValueError:
            raise Refusal(f"{part!r} is not an EMA period.", status_code=400) from None
        if not 1 <= period <= MAX_EMA_PERIOD:
            raise Refusal(
                f"EMA period {period} is outside 1-{MAX_EMA_PERIOD}.",
                status_code=400,
            )
        out.append(period)
    return out or list(EMA_PERIODS)


def _mt5_timeframe(timeframe: str) -> str:
    from backend.src.api.errors import Refusal
    try:
        return TIMEFRAMES[timeframe]
    except KeyError:
        raise Refusal(
            f"Unknown timeframe {timeframe!r}. Known: {', '.join(TIMEFRAMES)}.",
            status_code=400,
        )


def _placed(fvgs: list[dict], rows: list[dict]) -> list[dict]:
    """Give each gap the timestamp of the candle it was found on, and drop any
    gap whose index is not in this window.

    The detector reports an index into the candle series; the browser plots
    against time, so the index has to be resolved somewhere. Here keeps it
    private to the two parties that agree what it means.

    The drop is the point. A gap with no timestamp cannot be drawn, and a
    response that carries one fails validation for the whole payload — one
    stale index would cost the entire overlay, chart included, rather than the
    one zone it actually describes.
    """
    out = []
    for f in fvgs:
        idx = f.get("idx")
        if isinstance(idx, int) and 0 <= idx < len(rows):
            out.append({**f, "ts": float(rows[idx].get("ts") or 0)})
        else:
            log.warning("[chart] dropping a fair-value gap at index %r — outside "
                        "the %d-candle window it was requested for", idx, len(rows))
    return out


@router.get("/timeframes")
async def timeframes() -> dict:
    return {"timeframes": list(TIMEFRAMES), "ema_periods": EMA_PERIODS}


@router.get("/candles", response_model=list[Candle])
async def candles(
    timeframe: str = Query("5m"),
    count: int = Query(200, ge=10, le=1000),
    eng: Any = Depends(engine_dep),
) -> list[dict]:
    return await eng.get_candles(_mt5_timeframe(timeframe), count)


@router.get("/tick", response_model=TickOut | None)
async def tick(eng: Any = Depends(engine_dep)) -> dict | None:
    t = await eng.get_tick()
    return t.to_dict() if t else None


@router.get("/overlays", response_model=Overlays)
async def overlays(
    timeframe: str = Query("5m"),
    count: int = Query(200, ge=10, le=1000),
    emas: str | None = Query(None, description="Comma-separated EMA periods; "
                                               "defaults to the Chart tab's 9,21,50"),
    eng: Any = Depends(engine_dep),
) -> dict:
    periods = _ema_periods(emas)
    mt5_tf = _mt5_timeframe(timeframe)
    rows = await eng.get_candles(mt5_tf, count)
    closes = [float(c.get("close") or 0) for c in rows]
    fvgs = _placed(chart_ctl.select_display_fvgs(rows, chart_ctl.detect_fvgs(rows)), rows)
    return {
        "timeframe": timeframe,
        "count": len(rows),
        "emas": {str(p): chart_ctl.ema_series(closes, p) for p in periods},
        "rsi": chart_ctl.rsi_series(closes, 14),
        "fvgs": fvgs,
    }


@router.get("/trades", response_model=list[ChartTrade])
async def open_trades(eng: Any = Depends(engine_dep)) -> list[dict]:
    return await chart_ctl.get_open_trades(eng)


@router.get("/context")
async def context() -> dict:
    """Who is trading and on what risk settings — the two things the chart's
    header shows. One call, because they are read together on every refresh."""
    return {
        "active_trader": chart_ctl.get_active_trader(),
        "risk": chart_ctl.get_risk_settings(),
    }
