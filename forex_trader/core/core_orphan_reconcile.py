"""Close app rows whose broker position is already gone (2026-08-04).

THE GAP
-------
The app learns an EA-managed trade closed from the EA's own trade_closed
message. If the EA was not tracking that position at the time -- which,
before restore_trade() existed, was true of EVERY open position after any
EA reload -- that message never arrives, and the row stays 'open' with its
original remaining_lots and a $0 P&L indefinitely.

Confirmed live 2026-08-04, ticket 1704757612: a recompile orphaned it at
15:30, it closed at the broker at 16:13 for +$35.00, and
vantage_simulated_trades still read status='open' remaining_lots=0.1
net_pnl=0 afterwards. The Reversal Engine's own re_signals row reconciled
correctly, so the two halves of the app actively disagreed.

ea_bridge.restore_trade() prevents new occurrences. This repairs rows that
are ALREADY stranded, and catches any future case where the EA's message is
lost for some other reason (dropped socket mid-close, app restart across
the close, a position closed by hand in the terminal).

DELIBERATELY CONSERVATIVE. Closing a row that is genuinely still open would
free the app to re-enter a position it already holds, which is far worse
than leaving a stale row for another minute. So a row is only closed when
BOTH: the broker reports no such open position, AND the broker's own deal
history shows a real exit deal to take the close price and P&L from. A row
we cannot positively confirm as closed is left completely alone.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

# A just-opened trade can briefly be absent from /positions while the broker
# registers it. Well past any observed lag, and irrelevant to the stranded
# rows this exists for -- those sit for hours.
_MIN_AGE_S = 120.0


async def reconcile_orphaned_trades(bridge: Any, close_trade_fn) -> int:
    """Returns how many rows were repaired. Safe to call on a timer."""
    def _fetch():
        with db_module.db() as conn:
            return [
                db_module.row_to_dict(r) for r in conn.execute(
                    "SELECT trade_id, mt5_ticket, direction, entry_price, "
                    "       remaining_lots, open_time, strategy, tg_source "
                    "FROM vantage_simulated_trades "
                    "WHERE status='open' AND mt5_ticket IS NOT NULL AND mt5_ticket > 0"
                ).fetchall()
            ]

    rows = await db_module.to_db_thread(_fetch)
    if not rows:
        return 0

    cutoff = time.time() - _MIN_AGE_S
    rows = [r for r in rows if float(r.get("open_time") or 0) < cutoff]
    if not rows:
        return 0

    try:
        positions = await bridge.get_positions()
    except Exception as e:
        log.debug("[OrphanReconcile] could not read positions: %s", e)
        return 0
    # An empty list is ambiguous -- a genuinely flat account and a failed
    # read look identical -- and acting on it would close every open row at
    # once. Refuse rather than risk that.
    if not positions:
        return 0

    live = {int(p.get("ticket") or 0) for p in positions}
    repaired = 0

    for row in rows:
        ticket = int(row["mt5_ticket"])
        if ticket in live:
            continue

        try:
            hist = await bridge.get_position_history(ticket)
        except Exception:
            continue
        exits = [d for d in (hist or []) if d.get("entry") == 1]
        if not exits:
            # Gone from /positions but with no exit deal to prove it closed.
            # Not confident enough to touch it.
            log.debug("[OrphanReconcile] ticket=%s absent from positions but has no "
                      "exit deal yet -- leaving alone", ticket)
            continue

        last = exits[-1]
        close_price = float(last.get("price") or 0)
        if close_price <= 0:
            continue
        real_net = sum(float(d.get("profit") or 0) + float(d.get("swap") or 0)
                       + float(d.get("fee") or 0) for d in exits)

        try:
            await close_trade_fn(row["trade_id"], "reconciled_broker_closed")
        except Exception as e:
            log.warning("[OrphanReconcile] close failed for trade=%s ticket=%s: %s",
                        str(row["trade_id"])[:8], ticket, e)
            continue

        # record_close prices the remaining lots at close_price using the
        # app's own fee model, which is an estimate. The broker's own deal
        # P&L is the truth, so overwrite with it -- otherwise a repaired row
        # reports a subtly different number from the account statement.
        def _apply_real(tid=row["trade_id"], net=real_net, cp=close_price):
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades "
                    "SET net_pnl=?, mt5_profit=?, close_price=?, exit_reason=? "
                    "WHERE trade_id=?",
                    (net, net, cp, "reconciled_broker_closed", tid),
                )
        try:
            await db_module.to_db_thread(_apply_real)
        except Exception as e:
            log.warning("[OrphanReconcile] wrote close but could not apply real P&L "
                        "for trade=%s: %s", str(row["trade_id"])[:8], e)

        repaired += 1
        log.warning(
            "[OrphanReconcile] repaired stranded row trade=%s ticket=%s (%s %s): broker "
            "closed it @ %.2f for %+.2f but no trade_closed ever reached the app",
            str(row["trade_id"])[:8], ticket, row.get("direction"),
            row.get("tg_source") or row.get("strategy") or "", close_price, real_net,
        )

    return repaired
