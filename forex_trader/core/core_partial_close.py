"""Partial-close DB accounting -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.partial_close_trade, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-partial-close-migration/020-*.md.

Never calls the MT5 bridge -- the actual broker-side partial close happens
at the (deferred) strategy-handler call site before this runs; this
function only records the DB-side bookkeeping afterward. Calls
core_fees_sizing.pnl() (pack 1) instead of self.pnl().
"""
from __future__ import annotations

import time

from forex_trader.core import database as db_module
from forex_trader.core.core_fees_sizing import pnl as _pnl


async def partial_close_trade(trade_id: str, lots_to_close: float,
                              close_price: float, reason: str = "TP") -> dict:
    def _fetch_row():
        with db_module.db() as conn:
            return db_module.row_to_dict(
                conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
            )
    row = await db_module.to_db_thread(_fetch_row)
    if not row or row["status"] != "open":
        raise ValueError(f"Trade {trade_id} is not open")

    direction   = row["direction"]
    remaining   = float(row["remaining_lots"])
    entry_price = float(row["entry_price"])
    lots_to_close = min(lots_to_close, remaining)
    partial_pnl   = _pnl(direction, entry_price, close_price, lots_to_close)
    new_remaining = round(remaining - lots_to_close, 4)
    now = time.time()

    def _apply_partial():
        with db_module.db() as conn:
            conn.execute(
                "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                " VALUES (?,?,?,?,?,?)",
                (trade_id, now, lots_to_close, close_price, partial_pnl, reason),
            )
            conn.execute(
                "UPDATE vantage_simulated_trades SET remaining_lots=?,realised_pnl=realised_pnl+?,"
                "net_pnl=net_pnl+? WHERE trade_id=?",
                (new_remaining, partial_pnl, partial_pnl, trade_id),
            )
            conn.execute(
                "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
                (partial_pnl,),
            )
            rs = db_module.get_risk_settings()
            if reason == "TP1" and rs.get("move_sl_to_be_after_tp1", 1) and not row["sl_moved_to_be"]:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
                    (entry_price, trade_id),
                )
            if new_remaining <= 0:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET status='closed',close_time=?,"
                    "exit_reason='all_tps_hit',close_price=?,remaining_lots=0 WHERE trade_id=?",
                    (now, round(close_price, 2), trade_id),
                )
                conn.execute(
                    "UPDATE vantage_signals SET status='closed' WHERE signal_id=?",
                    (row["signal_id"],),
                )
    await db_module.to_db_thread(_apply_partial)

    return {
        "trade_id":     trade_id,
        "lots_closed":  lots_to_close,
        "remaining_lots": new_remaining,
        "partial_pnl":  partial_pnl,
        "auto_closed":  new_remaining <= 0,
    }
