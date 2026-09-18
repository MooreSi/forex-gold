"""The money router. Every endpoint here can open, close or resize a position.

**Read `docs/system/rules/20-trading-safety.md` and `.claude/skills/safe-change/`
before changing anything in this file.**

Three rules hold it together, and none of them is a style preference:

1. **A handler is one engine call.** The argument list is the engine's
   signature, in the engine's order, with the engine's defaults. Nothing is
   computed, defaulted, clamped or renamed on the way through. The close path
   (`close_trade`, `partial_close_trade`) is frozen — golden rule 2 — and a
   forward that reshaped its arguments would be exactly the reshaping that rule
   forbids, wearing an HTTP handler's clothes.
2. **This layer never decides whether an order is allowed.** No lot ceiling, no
   halt check, no spread check. The engine decides and its answer — including
   its refusal text — is returned unchanged. A check here would be a second
   risk opinion that drifts from the real one.
3. **POST only.** A GET that opens a position is one prefetch away from an
   order nobody asked for.

The engine returns a dict on success and raises on refusal; both reach the
client intact — the refusal through `errors.Refusal`, so the user reads the
broker's real reason rather than "something went wrong".
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.api.schemas.trading import (
    CloseRequest, LimitOrderRequest, MarketOrderRequest,
    OpenFromSignalRequest, PartialCloseRequest,
)

router = APIRouter(prefix="/api/trading", tags=["trading", "money"])


def _refuse(exc: Exception) -> Refusal:
    """A ValueError from the order path is the engine saying no with a reason
    the user needs to read — 'DPM is disabled and no stop loss was given' is
    actionable; a 500 is not."""
    return Refusal(str(exc), status_code=409)


@router.post("/orders/market")
async def place_market_order(
    body: MarketOrderRequest, eng: Any = Depends(engine_dep),
) -> dict:
    try:
        return await eng.open_manual_market_order(
            body.direction,
            stop_loss=body.stop_loss,
            lot_size=body.lot_size,
            strategy=body.strategy,
            take_profit=body.take_profit,
            source_name=body.source_name,
        )
    except ValueError as exc:
        raise _refuse(exc) from exc


@router.post("/orders/limit")
async def place_limit_order(
    body: LimitOrderRequest, eng: Any = Depends(engine_dep),
) -> dict:
    try:
        return await eng.open_manual_limit_order(
            body.direction,
            body.entry_low,
            body.entry_high,
            body.stop_loss,
            tp1=body.tp1, tp2=body.tp2, tp3=body.tp3, tp4=body.tp4,
            tp5=body.tp5, tp6=body.tp6, tp7=body.tp7, tp8=body.tp8,
            lot_size=body.lot_size,
            notes=body.notes,
        )
    except ValueError as exc:
        raise _refuse(exc) from exc


@router.post("/trades/{trade_id}/close")
async def close_trade(
    trade_id: str, body: CloseRequest, eng: Any = Depends(engine_dep),
) -> dict:
    # Frozen path. Two positional arguments, in this order, and the default
    # reason spelled exactly as the engine spells it.
    try:
        return await eng.close_trade(trade_id, body.reason)
    except ValueError as exc:
        raise _refuse(exc) from exc


@router.post("/trades/{trade_id}/partial-close")
async def partial_close_trade(
    trade_id: str, body: PartialCloseRequest, eng: Any = Depends(engine_dep),
) -> dict:
    # Frozen path — see above.
    try:
        return await eng.partial_close_trade(
            trade_id, body.lots_to_close, body.close_price, body.reason,
        )
    except ValueError as exc:
        raise _refuse(exc) from exc


@router.post("/signals/{signal_id}/open")
async def open_from_signal(
    signal_id: str, body: OpenFromSignalRequest, eng: Any = Depends(engine_dep),
) -> dict:
    try:
        return await eng.open_trade_from_signal(
            signal_id,
            lot_size_override=body.lot_size_override,
            age_lot_mult=body.age_lot_mult,
        )
    except ValueError as exc:
        raise _refuse(exc) from exc


@router.post("/signals/{signal_id}/cancel")
async def cancel_signal(signal_id: str, eng: Any = Depends(engine_dep)) -> dict:
    try:
        return await eng.cancel_signal(signal_id)
    except ValueError as exc:
        raise _refuse(exc) from exc
