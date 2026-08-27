"""Handing state back to the EA when it reconnects.

On "hello" the EA knows nothing: it has just attached to the chart and has no
record of the trades or resting orders this app is managing. These four
methods replay them, so a terminal restart does not leave positions untrailed
and limit orders unwatched.

Mixed into EABridge -- see this package's __init__. Split out verbatim.
"""
from __future__ import annotations

import json
import logging

from backend.src.services.broker import repo as broker_repo

log = logging.getLogger(__name__)


class RestoreMixin:
    """EABridge's reconnect-replay methods. Not instantiated on its own -- it
    uses self._send and calls back into the combined class."""

    async def restore_pending_order(self, row: dict) -> None:
        """Push one still-'working' vantage_pending_orders row back to the
        EA right after it reconnects (see _dispatch's "hello" handling).

        g_pending[] is pure in-memory state on the EA side with no
        persistence of its own -- any EA restart (recompile, terminal
        restart, a dropped socket that re-triggers OnInit) silently forgets
        every order that was still resting, and CheckPendingOrders() then
        has nothing left to check: it can never again notice that order's
        eventual fill or broker-side expiry. Confirmed live 2026-07-24: 5
        Limit Runner orders sat "pending" in the UI for 16+ hours after
        genuinely expiring on MT5 hours earlier, because whichever EA
        restart happened in between wiped them from tracking with no way
        for either side to notice afterward. Python is the durable source
        of truth for every field here, so pushing it back closes that gap
        regardless of why tracking was lost.

        Fire-and-forget: no ack is awaited. The EA's own reply -- nothing,
        for an order still genuinely resting; pending_order_filled/
        pending_order_cancelled for one that resolved while this EA was
        disconnected -- already routes through the normal _dispatch
        handlers, identical to a live fill/cancel event."""
        tps  = json.loads(row["tps_json"])
        pcts = json.loads(row["pcts_json"])
        msg = {
            "type": "restore_pending_order",
            "trade_id": row["trade_id"],
            "ticket": row["ea_ticket"],
            "direction": row["direction"],
            "lot_size": row["lot_size"],
            "stop_loss": row["stop_loss"],
            "strategy": row["strategy"],
            "be_at_pos": row["be_at_pos"],
            "close_full_on_last": 0 if row.get("tp_open") else 1,
        }
        for n_str, price in tps.items():
            msg[f"tp{n_str}"] = price
        for i, p in enumerate(pcts, start=1):
            msg[f"pct{i}"] = p
        await self._send(msg)

    async def _restore_pending_orders(self) -> None:
        """Called once per EA connection (on "hello") -- restores every
        still-'working' pending order so a prior EA restart can't leave any
        of them permanently untracked. See restore_pending_order()."""
        from backend.src.db import database as db_module

        from backend.src.services.broker import repo as broker_repo
        rows = await db_module.to_db_thread(broker_repo.fetch_working_pending_orders)
        for row in rows:
            try:
                await self.restore_pending_order(row)
            except Exception as e:
                log.warning("[EABridge] restore_pending_order failed for trade_id=%s: %s",
                            row.get("trade_id"), e)

    async def restore_trade(self, row: dict) -> None:
        """Push one still-open EA-managed POSITION back to the EA after it
        reconnects.

        g_trades[] has exactly the same no-persistence problem
        restore_pending_order() documents for g_pending[], but for live
        positions rather than resting orders, and it went unclosed until
        2026-08-04. Any EA restart (recompile, terminal restart, dropped
        socket re-triggering OnInit) silently forgot every open position:
        no partial closes, no breakeven, no trailing, and -- because the
        app learns a trade closed from the EA's own trade_closed message --
        no close notification either, so the row stayed 'open' in
        vantage_simulated_trades forever.

        Confirmed live 2026-08-04, ticket 1704757612: a recompile at 15:30
        orphaned it, it closed at the broker at 16:13 for +$35, and the
        trades table still read status='open' remaining_lots=0.1 net_pnl=0
        afterwards. That combination got worse, not better, once
        close_full_on_last=false legitimately started leaving positions with
        NO broker-side TP -- an orphan then has nothing at all to close it.

        Sends the template payload fresh from the DB rather than anything
        cached, so a restored trade is managed by the template's CURRENT
        settings, same as set_template already does for live ones.
        """
        from backend.src.services.broker import ea_templates as ea_templates
        from backend.src.services.trading.open_trade import (
            _EA_LADDER_PCTS, _EA_LADDER_BE_AT_POS, _EA_LADDER_TRAIL_MODE,
        )

        strategy = row.get("strategy") or ""
        msg = {
            "type": "restore_trade",
            "trade_id": row["trade_id"],
            "ticket": int(row["mt5_ticket"]),
            "direction": (row.get("direction") or "").upper(),
            "entry_price": float(row.get("entry_price") or 0),
            "orig_lots": float(row.get("lot_size") or 0),
            # What is left NOW. The EA uses this to work out how much of the
            # ladder already fired, so restoring cannot re-run a partial
            # close that has already happened.
            "remaining_lots": float(row.get("remaining_lots") or 0),
            "stop_loss": float(row.get("stop_loss") or 0),
            "strategy": strategy,
        }
        for n in range(1, 9):
            v = row.get(f"tp{n}")
            if v:
                msg[f"tp{n}"] = float(v)

        if ea_templates.is_template_override(strategy):
            tpl = ea_templates.get_ea_template(
                ea_templates.template_name_from_override(strategy))
            if tpl:
                for k, v in tpl.items():
                    if k in ("name", "created_at", "updated_at"):
                        continue
                    msg[f"tpl_{k}"] = (1 if v else 0) if isinstance(v, bool) else v
                pcts = [float(tpl.get(f"tp{n}_pct", 0) or 0) / 100.0 for n in range(1, 9)]
                for i, p in enumerate(pcts, start=1):
                    msg[f"pct{i}"] = p
        elif strategy in _EA_LADDER_PCTS:
            _table = _EA_LADDER_PCTS[strategy]
            _n_tps = sum(1 for n in range(1, 9) if row.get(f"tp{n}"))
            for i, p in enumerate(_table.get(_n_tps, _table[max(_table)]), start=1):
                msg[f"pct{i}"] = p
            msg["be_at_pos"] = _EA_LADDER_BE_AT_POS[strategy]
            if _EA_LADDER_TRAIL_MODE.get(strategy):
                msg["trail_mode"] = _EA_LADDER_TRAIL_MODE[strategy]

        await self._send(msg)

    async def _restore_open_trades(self) -> None:
        """Called once per EA connection (on "hello"), alongside
        _restore_pending_orders. See restore_trade()."""
        from backend.src.db import database as db_module

        rows = await db_module.to_db_thread(broker_repo.fetch_open_ea_managed_trades)
        if not rows:
            return
        for row in rows:
            try:
                await self.restore_trade(row)
            except Exception as e:
                log.warning("[EABridge] restore_trade failed for trade_id=%s: %s",
                            row.get("trade_id"), e)
        log.info("[EABridge] pushed %d open position(s) back to the EA after reconnect",
                 len(rows))
