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

from backend.src.services.risk import lot_sizing
from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.close_trade import get_trading_balance
from backend.src.services.trading.fees_sizing import suggest_lot_size
from backend.src.services.broker import ea_templates
from backend.src.services.trading import template_levels
from backend.src.services.trading.open_trade import resolve_template_tps
from backend.src.services.trading.limit_order_signal import _limit_runner_pcts, management_shape
from backend.src.services.trading.manual_market_order import SINGLE_TP_STRATEGY
from backend.src.services.risk.strategy_params import get_strategy_params
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER, STRATEGY_NAMES

__all__ = ["open_manual_limit_order", "SINGLE_TP_STRATEGY"]

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
    strategy: Optional[str] = None,
) -> dict:
    """`strategy` None is Limit Runner, as before; the dialog sends one
    (owner, 2026-10-07): SINGLE_TP_STRATEGY for its blank choice, a built-in
    strategy, or a single-mode EA template, which the EA runs on a resting
    order since v1.07 (ApplyTemplateToPending). A template's own ladder,
    measured from the resting price, replaces the typed take profit, as
    open_trade's does for the market order."""
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"Invalid direction: {direction}")
    strategy = strategy or STRATEGY_LIMIT_RUNNER
    template: Optional[dict] = None
    if ea_templates.is_template_override(strategy):
        if ea_templates.is_grid_template(strategy):
            raise ValueError(
                "A grid EA template stages its own legs and cannot rest as one "
                "limit order — choose a single-mode template or a strategy."
            )
        template = ea_templates.get_ea_template(
            ea_templates.template_name_from_override(strategy))
        if template is None:
            raise ValueError(f"EA template not found: {strategy}")
    elif strategy not in STRATEGY_NAMES:
        raise ValueError(f"Unknown strategy: {strategy}")

    from backend.src.services.broker import ea_bridge as _ea_mod
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
    price = entry_high if direction == "BUY" else entry_low
    if template is not None:
        _atr = await template_levels.template_atr(template, bridge)
        _tpl_tps, _, _ = resolve_template_tps(
            template, direction, template_levels.PriceRef(price),
            [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8], "Manual", atr=_atr)
        if _tpl_tps:
            tps = {int(k): float(v) for k, v in _tpl_tps.items()}
    if not tps:
        raise ValueError("At least TP1 is required.")

    rs = db_module.get_risk_settings()
    strategy_lot = lot_sizing.global_fixed_lot(rs)  # 0 unless Fixed lots mode
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
    trail_mode = None
    if strategy == STRATEGY_LIMIT_RUNNER or template is not None:
        # A template is managed from its tpl_* fields; these are the inert
        # placeholders limit_order_signal sends it too.
        be_at_pos = max(int(params.get("be_at_pos", 1)) - 1, 0)
        pcts      = _limit_runner_pcts(len(tps), False, params)
    else:
        _, pcts, be_at_pos, trail_mode = management_shape(strategy, len(tps), False, params)

    trade_id = str(uuid.uuid4())[:16]
    ack = await _ea.place_pending_order(
        trade_id, direction, price, lot, stop_loss, tps, pcts, be_at_pos,
        strategy=strategy,
        expire_minutes=_DEFAULT_EXPIRE_MINUTES,
        close_full_on_last=True,
        trail_mode=trail_mode,
        template=template,
    )
    if ack.get("type") != "pending_order_placed":
        raise RuntimeError(f"Limit order rejected by EA — {ack.get('error', 'unknown error')}")

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    trade_repo.insert_pending_order_signal(
        signal_id, "Manual Limit Order", direction, entry_low, entry_high,
        stop_loss, tps, lot,
        notes or f"Manual limit order @ {price:.2f} (EA ticket {ticket})",
        now, None,
        (trade_id, signal_id, None, "Manual", direction, price, stop_loss,
         json.dumps(tps), json.dumps(pcts), be_at_pos, 0,
         lot, ticket, "working", now, strategy),
    )

    _tg_text = (
        f"*Manual Limit Order Placed*\n"
        f"Direction: {direction}  |  Lots: {lot}\n"
        f"Price: ${price:.2f}  |  SL: ${stop_loss:.2f}\n"
        f"MT5 Ticket: {ticket}\n"
        f"Strategy: {'Single take profit' if strategy == SINGLE_TP_STRATEGY else STRATEGY_NAMES.get(strategy, strategy)}"
    )
    asyncio.create_task(telegram_alerts.send_message(_tg_text, trade_id, "manual_limit_open"))

    return {"trade_id": trade_id, "signal_id": signal_id, "mt5_ticket": ticket, "price": price, "lot_size": lot}
