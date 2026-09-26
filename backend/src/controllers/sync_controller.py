"""Cluster-sync API for the frontend: link state, remote commands, stats facades.

The pages used to import `services.cluster.sync.client` and hold the live
`SyncClient` object, reading `cli.conn_state` and `cli.remote_status` straight
off it. That put a service object in the UI's hands: any attribute the client
grew became UI surface, and a rename inside the client broke pages.

`link_state()` is the fix -- one plain dict, built once, with the four fields
the pages actually render. Everything else here is a named command. Nothing
returns a service object except `make_stats_facades`, which deliberately does:
the facades exist to be duck-typed against each engine's own module API so the
panels need no per-call-site change, and re-wrapping them would defeat the
whole point of that module.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.cluster import handover as _handover
from backend.src.services.cluster.sync import client as _client
from backend.src.services.cluster.sync import _mt5_accounts_sync as _accounts
from backend.src.services.cluster.sync import remote_stats_facade as _facade
from backend.src.services.cluster.sync import server as _server
from backend.src.services.cluster.sync import tls_util as _tls
from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS

__all__ = [
    "TRADER_REMOTE_VPS", "DEFAULT_SYNC_PORT",
    "link_state", "is_connected",
    "load_config", "configure", "start", "stop",
    "send_engine_control", "send_market_order", "request_model_snapshot",
    "request_stand_down", "request_resume", "push_ai_config", "push_mt5_accounts",
    "take_over_locally", "take_over_without_peer", "hand_back_to_remote",
    "HandoverRefused",
    "get_remote_open_position",
    "make_stats_facades",
    "cert_fingerprint", "server_start", "server_stop", "server_is_running",
]

DEFAULT_SYNC_PORT = _tls.DEFAULT_SYNC_PORT


# ── Link state ───────────────────────────────────────────────────────────────

def link_state() -> dict:
    """The peer link as plain values: what the pages render, nothing more.

    No None branch, and none is needed: `client.get_instance()` lazily
    constructs a SyncClient on first call and never returns None. Every
    `if cli is None:` guard in the pages this replaces was unreachable --
    an unconfigured link reports conn_state "disconnected" instead.
    """
    cli = _client.get_instance()
    return {
        "conn_state":      cli.conn_state,
        "last_error":      cli.last_error,
        "remote_status":   dict(cli.remote_status),
        "remote_settings": dict(cli.remote_settings),
    }


# `note_remote_setting` was here so a page could write the peer's ack into the
# confirmed snapshot -- without it the next click recomputes the same "current"
# and the toggle sticks one-directional (confirmed live: Bounce stuck OFF,
# Breakout stuck ON). The browser cannot do that write, so it moved with the
# rest of the routing into services/cluster/remote_control.py, which is also
# the only place that knows a write went to the peer at all.


def load_config() -> tuple[str, int, str]:
    return _client.SyncClient.load_config()


def configure(host: str, port: int, token: str) -> None:
    _client.get_instance().configure(host, int(port), token)


def start(host: str, port: int, token: str) -> None:
    _client.get_instance().start(host, int(port), token)


def stop() -> None:
    _client.get_instance().stop()


def is_connected() -> bool:
    """The check every page front-loads before sending a command."""
    return _client.get_instance().conn_state == "connected"


# ── Commands sent to the paired node ─────────────────────────────────────────

async def send_engine_control(engine: str, action: str, **kwargs):
    return await _client.get_instance().send_engine_control(engine, action, **kwargs)


async def send_market_order(direction: str, stop_loss: Optional[float] = None, **kwargs):
    return await _client.get_instance().send_market_order(
        direction, stop_loss=stop_loss, **kwargs)


async def request_model_snapshot(direction: str, timeout: float = 60.0) -> None:
    return await _client.get_instance().request_model_snapshot(direction, timeout=timeout)


async def request_stand_down(timeout: float = 15.0) -> dict:
    return await _client.get_instance().request_stand_down(timeout=timeout)


async def request_resume(timeout: float = 15.0) -> None:
    return await _client.get_instance().request_resume(timeout=timeout)


# ── Handing trading control over ─────────────────────────────────────────────
# The ORDER inside these is the safety property -- exactly one node may execute
# new trades against the shared account. It lives in services/cluster/handover.py,
# which is where the sequence and its failure behaviour are documented.

HandoverRefused = _handover.HandoverRefused


async def take_over_locally(*args, **kwargs) -> dict:
    return await _handover.take_over_locally(*args, **kwargs)


async def push_mt5_accounts() -> None:
    """This machine's MT5 accounts and demo/live choice, to its VPS."""
    return await _accounts.push_from_this_node()


async def take_over_without_peer() -> dict:
    return await _handover.take_over_without_peer()


async def hand_back_to_remote(*args, **kwargs) -> dict:
    return await _handover.hand_back_to_remote(*args, **kwargs)


async def push_ai_config(updates: dict) -> None:
    return await _client.get_instance().push_ai_config(updates)


def get_remote_open_position(mt5_ticket) -> Optional[dict]:
    return _client.get_instance().get_remote_open_position(mt5_ticket)


# ── Mode + stats ─────────────────────────────────────────────────────────────

# `is_remote_active` and `is_centralized_remote_mode` were here for the engine
# panels. Under React the panel cannot branch on them usefully on its own --
# the command has to be ROUTED, not just labelled -- so both live in
# services/cluster/remote_control.py with the routing they inform, and the API
# layer asks that one question ("where will this land?") instead of two.


def make_stats_facades(key: str, db_module, ml_module=None, params_module=None):
    return _facade.make_facades(key, db_module, ml_module, params_module)


# ── Server side (VPS) ────────────────────────────────────────────────────────

def cert_fingerprint() -> str:
    return _tls.cert_fingerprint()


async def server_start(host: str, port: int, token: str, *, main_engine=None,
                       breakout_engine=None, bounce_engine=None,
                       re_engine=None) -> None:
    """Build the sync server and start listening.

    init() and start() are one operation from the page's point of view, and
    keeping them one here is the point: the page never holds the SyncServer,
    so it cannot start a half-built one or stash a stale handle across a
    Local/Remote toggle.
    """
    srv = _server.init(main_engine=main_engine, breakout_engine=breakout_engine,
                       bounce_engine=bounce_engine, re_engine=re_engine)
    await srv.start(host, int(port), token)


async def server_stop() -> None:
    srv = _server.get_instance()
    if srv is not None:
        await srv.stop()


def server_is_running() -> bool:
    return _server.is_listening()
