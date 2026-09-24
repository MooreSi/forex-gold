"""The London opening-range breakout report, and its one-press execution.

Classic ORB: the whole Asian session (00:00-08:00 UTC) is a confirmation
filter, the first fifteen minutes of London (08:00-08:15 UTC) is the traded
opening range, and a breakout only counts once price clears BOTH in the same
direction. Stop at the opening range's midpoint, target at twice the resulting
risk. A 3:1 level is shown alongside it and is informational only -- the
automated path closes fully at the target and manages no partial ladder.

Restored 2026-09-18. The NiceGUI Trading page had this card and the React port
dropped it, so the report, its chart, the Execute button, the lot size and the
unattended auto-execute were all unreachable.

**`/execute` places a real market order.** It is the only endpoint in this
module that does, it is a POST, and it goes through the node routing in
`services/cluster/remote_control.py` -- in Remote mode the order belongs on the
machine holding the account, not on the one being looked at.

The chart is rendered server-side and returned base64. The browser cannot build
it: it is matplotlib over the same candles the report was computed from, and a
second implementation in TypeScript would be a second chance to disagree with
the numbers printed beside it.
"""
from __future__ import annotations

import asyncio

import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.controllers import engines_controller as engines_ctl
from backend.src.controllers import notifications_controller as notify_ctl
from backend.src.controllers import trading_controller as trading_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/orb", tags=["trading", "money"])

# The strategy every ORB execution is tagged with, so these trades can be told
# apart from a plain manual market order in the history.
STRATEGY = "orb_fixed"
SOURCE_NAME = "ORB/IVB Report"


class OrbSettings(BaseModel):
    """`lot_size` 0 means "size it from the risk percentage and the stop"."""

    lot_size: float | None = None
    auto_execute: bool | None = None


class OrbExecute(BaseModel):
    # Echoed back from the report the operator was looking at, so an execution
    # cannot be computed from one report and placed against another.
    direction: str
    stop_loss: float
    take_profit: float


@router.get("")
async def state() -> dict:
    """The report, its chart and the two settings, in one read.

    Returns `report: null` before London opens or when candles are not
    available. That is a real state and the tab says so -- an empty card would
    read as a failure.
    """
    try:
        report = await notify_ctl.build_orb_report()
    except Exception as exc:
        log.warning("[orb] could not build the report: %s", exc)
        raise Refusal(f"Could not build the ORB report: {exc}") from exc

    chart = None
    if report:
        try:
            # Off the loop: a matplotlib render, ~1.8 s measured.
            png = await asyncio.to_thread(notify_ctl.build_orb_chart_image, report)
            chart = base64.b64encode(png).decode() if png else None
        except Exception as exc:
            # The numbers are the point; the picture is not. A chart that will
            # not render must not take the report down with it.
            log.warning("[orb] chart render failed: %s", exc)

    settings = trading_ctl.get_risk_settings() or {}
    return {
        "report": report,
        "chart_png_base64": chart,
        "lot_size": float(settings.get("orb_lot_size") or 0.0),
        "auto_execute": bool(settings.get("orb_auto_execute_enabled") or 0),
        # Where an execution would land, so the button can say so before it is
        # pressed rather than after.
        "control_target": engines_ctl.control_target(),
    }


@router.put("/settings")
async def save_settings(body: OrbSettings) -> dict:
    """The lot size and the unattended auto-execute.

    Both are ordinary risk settings; they are here rather than on the Risk tab
    because they mean nothing away from this report.
    """
    updates: dict = {}
    if body.lot_size is not None:
        if body.lot_size < 0:
            raise Refusal("A lot size cannot be negative. Use 0 to size from "
                          "the risk percentage.", status_code=400)
        updates["orb_lot_size"] = float(body.lot_size)
    if body.auto_execute is not None:
        updates["orb_auto_execute_enabled"] = 1 if body.auto_execute else 0
    if not updates:
        raise Refusal("Nothing to save.")

    trading_ctl.update_risk_settings(updates)
    settings = trading_ctl.get_risk_settings() or {}
    return {
        "lot_size": float(settings.get("orb_lot_size") or 0.0),
        "auto_execute": bool(settings.get("orb_auto_execute_enabled") or 0),
    }


@router.post("/execute")
async def execute(body: OrbExecute, eng: Any = Depends(engine_dep)) -> dict:
    """**Open the breakout trade at market.** Real money.

    The stop and target come from the request rather than being recomputed
    here, so what is placed is what the operator was shown. The report moves as
    price does; re-reading it inside this handler would open a trade against
    numbers that were never on screen.

    A lot size of 0 (the default) means "size it from the risk percentage and
    the stop distance", which is what passing `None` to the engine does.
    """
    direction = body.direction.strip().upper()
    if direction not in ("BUY", "SELL"):
        raise Refusal(f"Unknown direction {body.direction!r}.", status_code=400)

    stored = float((trading_ctl.get_risk_settings() or {}).get("orb_lot_size") or 0.0)
    try:
        return await engines_ctl.place_market_order(
            eng,
            direction=direction,
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            lot_size=stored if stored > 0 else None,
            strategy=STRATEGY,
            source_name=SOURCE_NAME,
        )
    except engines_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc)) from exc
    except ValueError as exc:
        # The engine saying no with a reason the operator needs to read.
        raise Refusal(str(exc)) from exc
