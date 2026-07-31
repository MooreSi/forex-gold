"""Manual market order placement -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.open_manual_market_order, as part
of the core/engine.py migration series. See
docs/todo/refactor/core-manual-market-order-migration/020-*.md.

Calls open_trade (pack 11) -- the real MT5 order-placement call, unchanged
from the original. This module places no order itself; it only calls
whatever `bridge` its caller supplies.

Takes `bridge` as an explicit parameter instead of self._bridge/
self.get_fresh_tick()/self.get_candles(). Reuses core_fees_sizing.
suggest_lot_size (pack 1), core_close_trade.get_trading_balance (pack 10),
core_open_trade.open_trade (pack 11). background_open_commentary taken as
an optional injected async callable (default no-op), same pattern as
pack 13.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from backend.src.services.dpm import engine as dpm_engine
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.core_close_trade import get_trading_balance
from forex_trader.core.core_fees_sizing import suggest_lot_size
from forex_trader.core.core_open_trade import open_trade
from backend.src.utils.models import STRATEGY_SCALE_OUT, STRATEGY_NAMES


async def open_manual_market_order(
    bridge: Any,
    direction: str,
    stop_loss: Optional[float] = None,
    lot_size: Optional[float] = None,
    strategy: Optional[str] = None,
    take_profit: Optional[float] = None,
    source_name: str = "manual_market",
    starting_balance: float = 1000.0,
    background_open_commentary: Optional[Callable[[str, dict, Any], Awaitable[None]]] = None,
) -> dict:
    """
    Place an immediate market order from the UI without a pre-existing signal.

    take_profit/source_name: used by the ORB/IVB Report tab's Execute
    Trade button to pass its own computed target and tag the trade
    distinctly from a plain Market Order — every other caller leaves
    these at their defaults (no TP, "manual_market" tag), unchanged from
    before this was added.

    - direction: 'BUY' or 'SELL'
    - stop_loss: explicit SL price; if None and DPM is enabled, an ATR-based SL is
      calculated automatically.  If None and DPM is disabled, raises ValueError.
    - lot_size: explicit lots; if None, auto-calculated from risk settings.

    Returns the same dict as open_trade(): trade_id, mt5_ticket, entry_price, strategy.
    """
    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"Invalid direction: {direction}")

    rs = db_module.get_risk_settings()

    # Max-open-trades is enforced in open_trade() itself, against whichever
    # node actually executes — see that function for why it moved there.

    tick = await bridge.get_fresh_tick()
    if not tick:
        raise RuntimeError("No live price available")

    entry_px = tick.ask if direction == "BUY" else tick.bid

    # Resolve SL
    dpm_on = bool(rs.get("dpm_enabled", 0))
    if stop_loss and float(stop_loss) > 0:
        sl = float(stop_loss)
        # Sanity check on an explicit user-entered SL price — the mo_sl
        # UI field defaults to 0.0 with step=0.5 and no minimum-distance
        # validation, so a single accidental stepper click submits an
        # absolute SL of $0.50 (nonsensical for an instrument trading
        # near $4000+) as if it were a real price. Confirmed live
        # 2026-07-13: two manual orders placed with stop_loss=0.5,
        # effectively no real stop at all. Reject anything implying a
        # stop distance under $1 or over 10% of the entry price — real
        # strategies in this app use single or low-double-digit-dollar
        # distances, so both ends of that range comfortably bound any
        # intentional SL while catching a stray absolute-price value.
        _dist = abs(entry_px - sl)
        if _dist < 1.0 or _dist > entry_px * 0.10:
            raise ValueError(
                f"Stop Loss ${sl:.2f} is implausible for entry ${entry_px:.2f} "
                f"(distance ${_dist:.2f}) — check for a stray value in the SL field."
            )
    elif dpm_on:
        # ATR-based initial SL — DPM will move it once the trade is running
        try:
            candles = await bridge.get_candles("M5", 20)
            atr = dpm_engine.compute_atr(candles) if candles else None
        except Exception:
            candles = []
            atr = None
        # Fall back to a fixed distance if ATR is unavailable
        sl_dist = (atr * 1.2) if (atr and atr > 0) else 8.0
        sl = round(entry_px - sl_dist, 2) if direction == "BUY" else round(entry_px + sl_dist, 2)
    else:
        raise ValueError(
            "Stop Loss is required when DPM is not active. "
            "Enable DPM or enter an SL price."
        )

    # Resolve lot size
    strategy = strategy or rs.get("trade_strategy", STRATEGY_SCALE_OUT)
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if lot_size and float(lot_size) > 0:
        final_lot = round(float(lot_size), 2)
    elif strategy_lot > 0:
        final_lot = strategy_lot
    else:
        risk_pct  = float(rs.get("risk_per_trade_pct", 0.5))
        balance   = await get_trading_balance(bridge, starting_balance)
        final_lot = suggest_lot_size(entry_px, sl, balance, risk_pct)
    final_lot = max(0.01, round(final_lot, 2))

    # Create a backing signal record so foreign-key references work
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,tp1,
                lot_size,notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "Manual Market Order",
             direction, entry_px, entry_px, sl, take_profit,
             final_lot,
             "Manual market order placed from dashboard",
             "active", time.time(), time.time()),
        )

    result = await open_trade(
        bridge,
        signal_id=signal_id, direction=direction,
        entry_low=entry_px, entry_high=entry_px,
        stop_loss=sl, tp1=take_profit,
        lot_size=final_lot, tick=tick, strategy=strategy,
        tg_source=source_name,
    )

    # Telegram notification
    entry_actual = float(result.get("entry_price", entry_px))
    ticket       = result.get("mt5_ticket", "—")
    dpm_label    = "DPM" if dpm_on else STRATEGY_NAMES.get(strategy, strategy)
    sl_label     = f"{sl:.2f}" if sl else "none"
    tp_line      = f"\nTarget: ${take_profit:.2f}" if take_profit else ""
    _title       = "*ORB/IVB Trade Executed*" if source_name != "manual_market" else "*Manual Market Order Placed*"
    _tg_text = (
        f"{_title}\n"
        f"Direction: {direction}  |  Lots: {final_lot}\n"
        f"Entry: ${entry_actual:.2f}  |  SL: {sl_label}{tp_line}\n"
        f"MT5 Ticket: {ticket}\n"
        f"Mode: {dpm_label}"
    )
    if result.get("executed_remotely"):
        # The VPS actually placed this order (centralized signal
        # generation forwarded it there) — its trade_id/mt5_ticket refer
        # to the VPS's own DB row, not this node's, and _tg_text above
        # already reflects the real returned entry/ticket, so it's not
        # misleading; just skip scheduling commentary against a trade_id
        # that doesn't exist in this node's local DB. _handle_signal_order
        # on the VPS sends its own notification once it holds that row.
        asyncio.create_task(
            telegram_alerts.send_message(_tg_text, result["trade_id"], "manual_market_open")
        )
        return result

    asyncio.create_task(
        telegram_alerts.send_message(_tg_text, result["trade_id"], "manual_market_open")
    )

    if background_open_commentary:
        asyncio.create_task(background_open_commentary(
            result["trade_id"],
            {"signal_id": signal_id, "direction": direction,
             "entry_low": entry_px, "entry_high": entry_px,
             "stop_loss": sl},
            tick,
        ))
    return result
