"""Which node in a paired Mac/VPS install owns which job (M4 B9e).

Two mutual-exclusion checks that used to be @staticmethods on the runtime,
which is to say plain functions that had been parked on a class because
that is where the code they gated happened to live.

Both fail OPEN -- an unpaired or standalone install has no counterpart to
conflict with, and an error here must not silently kill trading or bot
control. That choice is load-bearing and is preserved exactly.
"""
from __future__ import annotations

import logging

import time

from backend.src.db import database as db_module


log = logging.getLogger(__name__)


def is_active_trader_node() -> bool:
    """Whether THIS node is the one currently allowed to execute real
    trades — mirrors open_trade()'s two-sided mutual-exclusion gate
    (server/VPS role via sync.server.is_standing_down(), client/Mac role
    via get_active_trader() == TRADER_REMOTE_VPS). Fails open (True) on
    any error or unpaired/standalone install, same as those gates.

    Used to decide whether this node should spend paid AI credits on
    Telegram-signal recovery — see _try_ai_signal_fallback. Both sides of
    a paired Mac/VPS run separate Telethon sessions on the same account
    and parse every message independently regardless of which one is
    active (confirmed live 2026-07-09: both nodes independently invoked
    the AI fallback on the same unrecognised messages, doubling AI cost
    and splitting the Reader Logic > AI review queue across two node-
    local DBs with no way to see or clear the other node's entries from
    either UI). Only the paid AI call is gated here — deterministic
    parsing/signal creation keeps running on both nodes exactly as
    before, so a promoted standby still has everything it needs.
    """
    try:
        from backend.src.services.cluster.sync import server as _sync_srv_mod
        _srv = _sync_srv_mod.get_instance()
        if _srv is not None and _srv.is_standing_down():
            return False
    except ImportError:
        pass
    try:
        from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
        from backend.src.services.cluster.sync.client import SyncClient
        _host, _, _ = SyncClient.load_config()
        if _host and db_module.get_active_trader() == TRADER_REMOTE_VPS:
            return False
    except ImportError:
        pass
    return True


def is_bot_command_authority() -> bool:
    """Whether THIS node should own Telegram getUpdates polling right now.

    Only one process may long-poll a given bot token at a time — a second
    concurrent poller gets 409 Conflict from Telegram. When this Mac/VPS
    pair is connected, both sides' engines run this loop unconditionally,
    so without this gate they fight over the same token forever (each
    side's 409-triggered deleteWebhook kicks the other, which then kicks
    back — the recurring conflict cycle seen in the logs). The standing-
    down side is already a view-only dashboard (see Settings > Remote
    Node), so it has nothing to execute commands against anyway — this
    just extends that same mutual-exclusion to Telegram control.
    An unpaired, standalone install has no counterpart to conflict with,
    so it always polls.
    """
    try:
        from backend.src.services.cluster.sync.protocol import TRADER_LOCAL, TRADER_REMOTE_VPS
        if db_module.get_app_config("sync_server_enabled") == "1":
            return db_module.get_active_trader() == TRADER_REMOTE_VPS
        from backend.src.services.cluster.sync.client import SyncClient
        host, _port, _token = SyncClient.load_config()
        if not host:
            return True  # standalone install, no counterpart to conflict with
        return db_module.get_active_trader() == TRADER_LOCAL
    except Exception:
        return True  # fail open rather than silently killing bot control


def engines_generate_here() -> bool:
    """Whether the Signal Generator engines (Breakout, Reversal, Trend PA)
    analyse on THIS node. docs/todo/010, owner 2026-09-29.

    They run on the node that trades, so the order leaves from the machine
    nearest the broker. The one exception is `centralized_signal_gen_enabled`,
    which keeps them on the Mac while the VPS trades and forwards each order
    (MSG_SIGNAL_ORDER). Exactly one node of a pair answers True in every mode;
    tests/core/test_active_node_parses_telegram.py pins the table.

    Before this, each engine returned at `is_remote_node()` unconditionally, so
    with centralized generation off the VPS never generated, and the Mac's
    orders were all refused by open_trade's stand-down gate: nothing traded.

    Fails CLOSED, unlike the two gates above. Those gate credits and bot
    polling; this one decides whether a second node places orders for the
    same setup, and `is_remote_node()` failed closed too.
    """
    try:
        from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
        centralized = bool(db_module.get_risk_settings().get("centralized_signal_gen_enabled"))
        vps_trades = db_module.get_active_trader() == TRADER_REMOTE_VPS
        if db_module.get_app_config("sync_server_enabled") == "1":
            return vps_trades and not centralized
        from backend.src.services.cluster.sync.client import SyncClient
        host, _port, _token = SyncClient.load_config()
        if host and vps_trades:
            return centralized
        return True
    except Exception as e:
        log.warning("[node_roles] cannot tell whether the engines generate here, so they do not: %s", e)
        return False
