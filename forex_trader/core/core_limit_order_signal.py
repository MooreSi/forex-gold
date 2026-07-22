"""Limit Runner (STRATEGY_LIMIT_RUNNER) placement -- the only strategy that
places a genuine broker-side pending order (BuyLimit/SellLimit via the EA)
instead of waiting for price to re-enter a zone and then filling at market.

Triggered exclusively by parse_limit_order_signal()'s "BUY/SELL [LIMITS]
GOLD @ high/low AREA" message layout (signal_parser.py), which
classify_and_parse() checks for ahead of every channel's own configured
parser_format -- so this fires for any channel, format-matched only, per
the explicit design decision behind this feature (2026-07-22).

Requires the EA bridge to be connected and healthy: unlike every other
strategy's open_trade() handoff, there is no Python-bridge fallback for a
genuine pending order -- if the EA is unavailable, this signal is skipped
with an explanatory skip_reason rather than silently falling back to a
different (misleading) execution model. The message still gets recorded in
vantage_tg_signals either way, same as every other skipped signal.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from forex_trader.core import database as db_module
from forex_trader.core.core_strategy_params import get_strategy_params
from forex_trader.core.models import MAX_TP, STRATEGY_LIMIT_RUNNER

log = logging.getLogger(__name__)

# MT5 GTC-with-expiration -- matches the existing Python-simulated zone-wait
# signals' own 4h pending-expiry convention (core_pending_signal_activation.py).
_DEFAULT_EXPIRE_MINUTES = 240


def _limit_runner_pcts(n: int, tp_open: bool, params: dict) -> list[float]:
    """Split evenly across `n` numeric TPs. If the signal carried a literal
    "TP OPEN" line, reserve runner_reserve_pct for the permanently-open
    runner leg (the ladder handler's close_full_on_last=False then leaves
    that share open indefinitely once the last numeric TP closes its own
    slice); otherwise split the full 100% (the last TP closes everything
    remaining regardless of its own pcts entry -- see run_tp_ladder)."""
    if tp_open:
        reserve = min(max(float(params.get("runner_reserve_pct", 25.0)) / 100.0, 0.0), 0.95)
        each = (1.0 - reserve) / n
    else:
        each = 1.0 / n
    return [each] * n


async def handle_limit_order_signal(
    parsed: dict,
    tg_id: str,
    channel_name: str,
    source_label: str,
    rs: dict,
    sess_ok: bool,
    per_signal_skip: bool,
    per_signal_skip_reason: str,
    skip_reason: str,
    get_trading_balance_fn: Callable[[], Awaitable[float]],
    suggest_lot_size_fn: Callable[[float, float, float, float], float],
) -> dict:
    """Places a genuine pending limit order via the EA for a parsed
    "BUY/SELL [LIMITS] GOLD @ high/low AREA" signal. Returns
    {'skip_reason': str} -- there is no 'executed'/'exec_lot'/'exec_price'
    equivalent since nothing has filled yet; the caller's Telegram alert
    reports the placement (or skip) via skip_reason, same as the existing
    "signal queued, will auto-activate" wording used by the zone-wait path
    for every other strategy.
    """
    if not sess_ok:
        return {"skip_reason": skip_reason}
    if per_signal_skip:
        return {"skip_reason": f"Auto-eval declined signal: {per_signal_skip_reason}"}

    from forex_trader.core import ea_bridge as _ea_mod
    _ea = _ea_mod.get_instance()
    if _ea is None or not _ea.is_ea_healthy():
        return {
            "skip_reason": (
                "Limit order skipped — EA not connected. Pending orders require "
                "a live EA bridge; there is no Python-bridge fallback for this strategy."
            ),
        }

    direction  = parsed["direction"].upper()
    entry_low  = float(parsed["entry_low"])
    entry_high = float(parsed["entry_high"])
    stop_loss  = float(parsed["stop_loss"])
    tps = {n: float(parsed[f"tp{n}"]) for n in range(1, MAX_TP + 1) if parsed.get(f"tp{n}") is not None}
    if not tps:
        return {"skip_reason": "Limit order skipped — signal has no TP levels."}

    n = len(tps)
    tp_open = bool(parsed.get("tp_open"))
    params = get_strategy_params(STRATEGY_LIMIT_RUNNER)
    be_at_pos = max(int(params.get("be_at_pos", 1)) - 1, 0)
    pcts = _limit_runner_pcts(n, tp_open, params)

    # Near edge of the quoted AREA -- the price side reached first as the
    # market approaches the zone from outside it (BUY: top of the zone,
    # SELL: bottom), same boundary price_in_entry_range() already treats as
    # "in zone" for the Python-simulated path (core_scan_messages_auto_execute.py).
    price = entry_high if direction == "BUY" else entry_low

    balance = await get_trading_balance_fn()
    lot = suggest_lot_size_fn(price, stop_loss, balance, float(rs.get("risk_per_trade_pct", 0.5)))
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if strategy_lot > 0:
        lot = strategy_lot

    trade_id = str(uuid.uuid4())[:16]
    try:
        ack = await _ea.place_pending_order(
            trade_id, direction, price, lot, stop_loss, tps, pcts, be_at_pos,
            strategy=STRATEGY_LIMIT_RUNNER,
            expire_minutes=_DEFAULT_EXPIRE_MINUTES,
            close_full_on_last=not tp_open,
        )
    except Exception as exc:
        log.warning("[LimitRunner] place_pending_order failed for tg_id=%s: %s", tg_id, exc)
        return {"skip_reason": f"Limit order failed — {exc}"}

    if ack.get("type") != "pending_order_placed":
        return {"skip_reason": f"Limit order rejected by EA — {ack.get('error', 'unknown error')}"}

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Auto ({source_label})", direction, entry_low, entry_high,
             stop_loss, tps.get(1), tps.get(2), tps.get(3), tps.get(4), tps.get(5),
             tps.get(6), tps.get(7), tps.get(8), lot,
             f"Limit Runner pending order @ {price:.2f} (EA ticket {ticket})",
             "pending", now),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='pending',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_id),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, tg_id, channel_name, direction, price, stop_loss,
             json.dumps(tps), json.dumps(pcts), be_at_pos, int(tp_open), lot, ticket,
             "working", now, STRATEGY_LIMIT_RUNNER),
        )
    log.info(
        "[LimitRunner] pending order placed tg_id=%s ticket=%s %s %.2f lots @ %.2f SL=%.2f tps=%s tp_open=%s",
        tg_id, ticket, direction, lot, price, stop_loss, tps, tp_open,
    )
    return {
        "skip_reason": (
            f"Limit order placed — {direction} {lot:g} lots @ {price:.2f} "
            f"(EA ticket {ticket}), SL {stop_loss:.2f}. Will notify on fill."
        ),
    }
