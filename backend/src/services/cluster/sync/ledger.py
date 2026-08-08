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
