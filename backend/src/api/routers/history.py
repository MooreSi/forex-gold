"""Analysis tab — what the account has actually done.

**One endpoint, not four.** The NiceGUI page had three panels each running its
own 15-second timer and each calling `get_deal_history()` independently: at
idle, `/history?days=365` ran 4.3 times a minute, and a single page load
produced 388 round-trips into the Wine-hosted MT5 bridge in 25 seconds
(bugs/030). A per-panel cache was the fix there. Here the shape does it: the
panels read one consolidated payload from one shared poll, so there is one
request per interval however many panels are on screen.

`days` is a query parameter rather than a field, because the heatmap and the
scorecard are genuinely different questions when the operator changes the
window, and caching them together under one key would serve one panel the
other's period.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers import system_controller as system_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/history", tags=["history"])

# The window the page offered. Anything is accepted up to ten years, which is
# what the old "days=3650" option meant — and what forced the WebSocket buffer
# from 1MB to 10MB when the whole equity curve was pushed in one payload. This
# endpoint returns aggregates, not a row per trade, so the size is bounded by
# the number of channels and the 7x24 grid rather than by how long the account
# has been running.
DAYS = Query(30, ge=1, le=3650)


class ChannelPause(BaseModel):
    source: str
    paused: bool


def _grid_rows(grid: dict) -> list[dict]:
    """The hourly P&L grid as rows, keyed by weekday and hour.

    The service returns `{(weekday, hour): {...}}`. A tuple is not a JSON key,
    so it becomes two fields — not a "0-13" string, which the browser would
    have to parse back and could get wrong in exactly one place.
    """
    rows = []
    for key, value in (grid or {}).items():
        try:
            weekday, hour = key
        except (TypeError, ValueError):
            log.warning("[history] skipping an hourly-grid key that is not "
                        "(weekday, hour): %r", key)
            continue
        rows.append({
            "weekday": int(weekday),
            "hour": int(hour),
            "session": history_ctl.session_for_hour(int(hour)),
            "pnl": float(value.get("pnl") or 0.0),
            "n": int(value.get("n") or 0),
            "avg": float(value.get("avg") or 0.0),
        })
    rows.sort(key=lambda r: (r["weekday"], r["hour"]))
    return rows


@router.get("/state")
async def state(days: int = DAYS, eng: Any = Depends(engine_dep)) -> dict:
    """Everything the Analysis tab renders, for one window."""
    performance = await eng.compute_mt5_performance(days)
    return {
        "days": days,
        # {} when the bridge could not answer. The panel says "no broker data"
        # rather than rendering a zero that reads as a flat month.
        "performance": performance or {},
        "hourly": _grid_rows(history_ctl.get_hourly_pnl_grid(days)),
        "channels": history_ctl.get_channel_scorecard(days),
        "ladder": history_ctl.strategy_ladder_reach(days),
    }


@router.get("/trades")
async def closed_trades(days: int = DAYS, eng: Any = Depends(engine_dep)) -> dict:
    """Every closed trade in the window, deal by deal.

    Its own endpoint rather than a field on `/state`: `/state` returns
    aggregates whose size is bounded by the channel count and a 7x24 grid,
    while this is a row per trade and ten years of them is what forced the
    old WebSocket buffer from 1MB to 10MB. A panel that is not on screen
    should not be paying for it.

    `error` is separate from an empty list because "no trades in this window"
    and "the bridge is down" look identical in an empty table.
    """
    return await history_ctl.closed_trade_table(eng, days)


@router.get("/today")
async def today() -> dict:
    """Today on the TRADING clock, not the machine's date.

    They differ whenever a clock offset is configured, which is the whole point
    on a VPS in another timezone — a calendar that highlighted the machine's
    today would mark the wrong day's trades.
    """
    return {"date": system_ctl.local_today().isoformat()}


@router.post("/recompute")
async def recompute(days: int = DAYS) -> dict:
    """Rebuild the channel performance table, then read it back.

    A button rather than a poll: it walks every trade in the window, which is
    not something to do every fifteen seconds behind the operator's back.
    """
    history_ctl.recompute_channel_performance(days)
    return {"channels": history_ctl.get_channel_scorecard(days)}


@router.put("/channel-paused")
async def set_channel_paused(body: ChannelPause) -> dict:
    """Stop taking a channel's signals without deleting anything.

    Not a money action in itself — it changes what the engines accept next
    time, and touches no open position.
    """
    history_ctl.set_channel_paused(body.source, body.paused)
    return {"source": body.source, "paused": body.paused}
