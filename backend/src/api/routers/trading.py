"""Trading tab — the read surface. Nothing here can move money.

Reads and writes live in separate routers on purpose. A GET that can place an
order is one browser prefetch, one link preview or one over-eager retry away
from an order nobody asked for; keeping the order endpoints in `orders.py`
means that mistake cannot be made by accident here.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.api.schemas.trading import (
    ChannelStrategyOverride, HaltState, RiskSettingsUpdate,
)
from backend.src.controllers import trading_controller as trading_ctl

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.get("/risk")
async def risk_settings() -> dict:
    return await trading_ctl.get_risk_settings_async()


@router.put("/risk")
async def update_risk_settings(body: RiskSettingsUpdate) -> dict:
    trading_ctl.update_risk_settings(body.model_dump())
    return await trading_ctl.get_risk_settings_async()


@router.get("/halt", response_model=HaltState)
async def halt_state() -> dict:
    """The three separate reasons trading can be off, read together.

    They are always displayed together and they are cheap, so one call keeps
    the browser from making three and rendering a moment where the market is
    closed but the breaker says fine.
    """
    return {
        "reason": trading_ctl.trading_pause_status()["reason"],
        "market_closed": bool(trading_ctl.is_weekly_market_closed()),
        "circuit_breaker": trading_ctl.get_circuit_breaker_state(),
    }


@router.get("/trades")
async def open_trades(eng: Any = Depends(engine_dep)) -> list[dict]:
    return await trading_ctl.get_open_trades(eng)


@router.get("/signals")
async def signals(
    status: Optional[str] = Query(None),
    eng: Any = Depends(engine_dep),
) -> list[dict]:
    return await trading_ctl.get_signals(eng, status)


@router.get("/signals/{signal_id}")
async def signal(signal_id: str) -> dict:
    return trading_ctl.get_signal(signal_id)


@router.get("/tg-signals")
async def tg_signals(
    limit: int = Query(50, ge=1, le=500),
    eng: Any = Depends(engine_dep),
) -> list[dict]:
    return await trading_ctl.get_tg_signals(eng, limit)


@router.get("/strategies")
async def strategies() -> dict:
    return {
        "catalogue": trading_ctl.build_strategy_catalogue(),
        "custom": trading_ctl.get_custom_strategies(),
    }


@router.get("/channel-strategies")
async def channel_strategies() -> dict:
    return trading_ctl.get_all_channel_strategy_settings()


@router.post("/channel-strategies")
async def set_channel_strategy(body: ChannelStrategyOverride) -> dict:
    trading_ctl.set_channel_strategy_override(body.source, body.strategy, body.auto)
    return trading_ctl.get_channel_strategy_rec(body.source)


@router.get("/config/{key}")
async def app_config(key: str) -> dict:
    return {"key": key, "value": trading_ctl.get_app_config(key)}


@router.put("/config/{key}")
async def set_app_config(key: str, body: dict) -> dict:
    trading_ctl.set_app_config(key, str(body.get("value", "")))
    return {"key": key, "value": trading_ctl.get_app_config(key)}

# ── Channel strategy recommendations ─────────────────────────────────────────

@router.post("/channel-strategies/recommend")
async def recommend_channel_strategies(body: dict) -> dict:
    """Ask for a strategy recommendation per channel, from that channel's own
    record. **Billable** when the configured provider is a paid model."""
    sources = body.get("sources") or []
    if not isinstance(sources, list) or not sources:
        raise Refusal("Name at least one channel to evaluate.", status_code=400)
    return {"billable": True,
            "recommendations": await trading_ctl.get_channel_strategy_recs(sources)}


@router.get("/channel-strategies/recommendations")
async def channel_strategy_recommendations(sources: str = Query("")) -> dict:
    """The recommendations already computed, from the local record. Free.

    Each carries its human label, so the browser never holds a mapping from
    strategy id to name — one place to change when an id is renamed, which is
    the whole reason the vocabulary is served as data rather than re-stated.
    `describe_strategy` falls back to the raw key, so a recommendation naming a
    strategy this build no longer has renders as itself instead of as blank.
    """
    wanted = [s for s in sources.split(",") if s]
    recs = trading_ctl.get_channel_strategy_rec_map(wanted)
    catalogue = trading_ctl.build_strategy_catalogue(include_hidden=True)
    labelled = {}
    for source, rec in (recs or {}).items():
        row = dict(rec) if isinstance(rec, dict) else {"strategy": rec}
        label, summary = trading_ctl.describe_strategy(
            str(row.get("strategy") or ""), catalogue)
        labelled[source] = {**row, "label": label, "summary": summary}
    return {"billable": False, "recommendations": labelled}


# ── Pending signals ──────────────────────────────────────────────────────────

@router.put("/signals/{signal_id}")
async def update_signal(signal_id: str, body: dict, eng: Any = Depends(engine_dep)) -> dict:
    """Edit ONE pending signal's levels.

    The signal id comes from the path, and an id in the BODY is discarded
    rather than merged. A row editor that took the id from its payload is one
    stale closure away from writing another row's values — the bug the NiceGUI
    editor needed explicit widget captures to avoid. Discarding it also stops
    the two colliding on the same keyword argument, which would surface as a
    500 rather than as the silent wrong-row write it is protecting against.
    """
    fields = {k: v for k, v in (body or {}).items()
              if k not in ("signal_id", "id")}
    await eng.update_signal(signal_id, **fields)
    return trading_ctl.get_signal(signal_id)


@router.delete("/tg-signals/{row_id}")
async def delete_tg_signal(row_id: int) -> dict:
    trading_ctl.delete_tg_signal_row(row_id)
    return {"deleted": row_id}
