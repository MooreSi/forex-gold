"""Manual limit-order placement -- the "Create Limit Order" form's actual
backend (Trading > Strategy > Limit Order tab).

Fixed 2026-07-24: the form was calling engine.create_signal() +
open_trade_from_signal(), the exact same "wait for price to re-enter the
zone, then fill at MARKET" path the automatic Telegram zone-signal handler
uses -- so "Save & Open Trade" only ever worked when price already
happened to sit inside Entry Low-High at the moment of the click, and
rejected with "price is above/below the entry zone" otherwise. That is
backwards for a genuinely resting limit order (the whole point is to
place it precisely when price is NOT there yet) and gave no way to place
a real broker-side pending order from the UI at all.

This module instead places a genuine BuyLimit/SellLimit via the EA --
same mechanism and Strategy Parameters (Limit Runner) as
core_limit_order_signal.py's automatic "[LIMITS]" Telegram path, just
without the tg_id/channel-name plumbing that path needs. Requires the EA
bridge connected and healthy, same as that path -- there is no
Python-bridge fallback for a genuine pending order.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.core_close_trade import get_trading_balance
from forex_trader.core.core_fees_sizing import suggest_lot_size
from forex_trader.core.core_limit_order_signal import _limit_runner_pcts
from backend.src.services.risk.strategy_params import get_strategy_params
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER

_DEFAULT_EXPIRE_MINUTES = 240


async def open_manual_limit_order(
    bridge: Any,
    direction: str,
    entry_low: float,
    entry_high: float,
    stop_loss: float,
    tp1: Optional[float] = None, tp2: Optional[float] = None,
    tp3: Optional[float] = None, tp4: Optional[float] = None,
    tp5: Optional[float] = None, tp6: Optional[float] = None,
    tp7: Optional[float] = None, tp8: Optional[float] = None,
    lot_size: Optional[float] = None,
    notes: str = "",
    starting_balance: float = 1000.0,
) -> dict:
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"Invalid direction: {direction}")

    from forex_trader.core import ea_bridge as _ea_mod
    _ea = _ea_mod.get_instance()
    if _ea is None or not _ea.is_ea_healthy():
        raise ConnectionError(
            "Limit order requires a connected, healthy EA bridge (Settings > "
            "MT5 / Bridge > EA Bridge) — there is no Python-bridge fallback "
            "for a genuine resting pending order."
        )

    tps = {n: v for n, v in enumerate(
        [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8], start=1
    ) if v is not None}
    if not tps:
        raise ValueError("At least TP1 is required.")

    price = entry_high if direction == "BUY" else entry_low

    rs = db_module.get_risk_settings()
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if lot_size and float(lot_size) > 0:
        lot = round(float(lot_size), 2)
    elif strategy_lot > 0:
        lot = strategy_lot
    else:
        # suggest_lot_size() applies Global Parameters > Max Risk per trade %
        # internally -- an explicit fixed lot (either branch above) always
        # wins and is never capped, matching every other sizing layer.
        risk_pct = float(rs.get("risk_per_trade_pct", 0.5))
        balance  = await get_trading_balance(bridge, starting_balance)
        lot = suggest_lot_size(price, stop_loss, balance, risk_pct)
    lot = max(0.01, round(lot, 2))

    params    = get_strategy_params(STRATEGY_LIMIT_RUNNER)
    be_at_pos = max(int(params.get("be_at_pos", 1)) - 1, 0)
    pcts      = _limit_runner_pcts(len(tps), False, params)

    trade_id = str(uuid.uuid4())[:16]
    ack = await _ea.place_pending_order(
        trade_id, direction, price, lot, stop_loss, tps, pcts, be_at_pos,
        strategy=STRATEGY_LIMIT_RUNNER,
        expire_minutes=_DEFAULT_EXPIRE_MINUTES,
        close_full_on_last=True,
    )
    if ack.get("type") != "pending_order_placed":
        raise RuntimeError(f"Limit order rejected by EA — {ack.get('error', 'unknown error')}")

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "Manual Limit Order", direction, entry_low, entry_high,
             stop_loss, tps.get(1), tps.get(2), tps.get(3), tps.get(4), tps.get(5),
             tps.get(6), tps.get(7), tps.get(8), lot,
             notes or f"Manual limit order @ {price:.2f} (EA ticket {ticket})",
             "pending", now),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, None, "Manual", direction, price, stop_loss,
             json.dumps(tps), json.dumps(pcts), be_at_pos, 0,
             lot, ticket, "working", now, STRATEGY_LIMIT_RUNNER),
        )

    _tg_text = (
        f"*Manual Limit Order Placed*\n"
        f"Direction: {direction}  |  Lots: {lot}\n"
        f"Price: ${price:.2f}  |  SL: ${stop_loss:.2f}\n"
        f"MT5 Ticket: {ticket}\n"
        f"Strategy: Limit Runner"
    )
    asyncio.create_task(telegram_alerts.send_message(_tg_text, trade_id, "manual_limit_open"))

    return {"trade_id": trade_id, "signal_id": signal_id, "mt5_ticket": ticket, "price": price, "lot_size": lot}
