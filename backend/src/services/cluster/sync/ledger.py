"""
Shared entry point every engine's close-path calls to push a closed trade
into the consolidated ledger — safe to call unconditionally from any engine
on any machine, whether or not Local/Remote sync is configured at all.

Always records into this node's own local ledger first (so the local Edge
Dashboard/History views work even if the sync link is down), then forwards
over whichever sync role is active on this process — SyncServer (VPS) or
SyncClient (Mac). Exactly one of those is ever active on a given machine;
checking both here means engines never need to know which role they're
running under.
"""
from __future__ import annotations

import asyncio
import logging

from backend.src.db import database as db_module

log = logging.getLogger("sync")


def push_trade_closed(trade: dict) -> None:
    """trade: {trade_id, engine, direction, strategy, open_time, close_time,
    pnl_dollars, outcome}. Fire-and-forget — never raises, never blocks the
    caller's close path on network I/O."""
    node_id = db_module.get_or_create_node_id()
    try:
        db_module.record_consolidated_trade(node_id, trade)
    except Exception as e:
        log.debug("[Ledger] local record failed: %s", e)
        return

    try:
        from backend.src.services.cluster.sync import server as sync_server
        srv = sync_server.get_instance()
        if srv is not None:
            asyncio.ensure_future(srv.push_own_trade_closed(trade))
            return
    except Exception as e:
        log.debug("[Ledger] server push failed: %s", e)

    try:
        from backend.src.services.cluster.sync import client as sync_client
        cli = sync_client.get_instance()
        if cli is not None and cli.conn_state == "connected":
            asyncio.ensure_future(cli.push_trade_closed(trade))
    except Exception as e:
        log.debug("[Ledger] client push failed: %s", e)


# Grading thresholds, as `close_trade.record_close` applies them inline when it
# first pushes the row. They are duplicated here rather than imported because
# close_trade is the frozen close path (CLAUDE.md rule 4) and routing its
# literal through a helper would reshape it. Recorded as a known duplication:
# if one moves, both move.
_WIN_AT  = 0.5
_LOSS_AT = -0.5


def grade_outcome(pnl_dollars: float) -> str:
    """"win" / "loss" / "be" for a settled P&L."""
    if pnl_dollars > _WIN_AT:
        return "win"
    if pnl_dollars < _LOSS_AT:
        return "loss"
    return "be"


def amend_trade_pnl(trade_id: str, pnl_dollars: float) -> None:
    """Correct an already-published ledger row's P&L, and re-grade it.

    `record_close` pushes the figure the close path could compute at the time.
    When `profit_sync` later gets the broker's own settled number and corrects
    `net_pnl`, the ledger was left holding the estimate -- normally pennies
    out, but bugs/025's priceless closes left three rows at ~-$44,800 against
    real losses of $264-$636. The ledger is what every cross-node P&L, win
    rate and Edge Dashboard figure reads.

    AMEND ONLY. A trade with no row of ours is left alone: pushing one from
    here would have to invent engine/direction/strategy, and a half-row in the
    cross-node ledger is worse than an absent one.

    Fire-and-forget, like `push_trade_closed` -- a correction that cannot be
    published must not break the sync that produced it.
    """
    try:
        node_id  = db_module.get_or_create_node_id()
        existing = db_module.get_consolidated_trade(node_id, trade_id)
    except Exception as e:
        log.debug("[Ledger] amend lookup failed for %s: %s", trade_id, e)
        return
    if not existing:
        return
    corrected = dict(existing)
    corrected["pnl_dollars"] = round(float(pnl_dollars), 4)
    corrected["outcome"]     = grade_outcome(float(pnl_dollars))
    # Through the same upsert + forward the close path uses, so the paired
    # node's copy is corrected too rather than only this one's.
    push_trade_closed(corrected)
