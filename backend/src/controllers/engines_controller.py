"""Engine LIFECYCLE: which engines exist, and starting and stopping them.

The Reversal Engine's own measurements -- its P&L, its ML gate, its virtual
trades -- moved to `reversal_controller` on 2026-09-19. This file had grown
two jobs, and the controller ceiling is what said so.

The panels used to reach `run_db(fn)` here, handing this layer an arbitrary
callable to run on the DB worker thread. That inverted the dependency: the
page chose the data access and the controller just dispatched it. Each of
those calls is now a named function on the owning engine's service.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.breakout_signal import panel_data as breakout
from backend.src.services.cluster import remote_control as _remote
from backend.src.services.engines import registry as _engines
from backend.src.services.risk import settings as _risk

__all__ = [
    "breakout", "get_risk_settings", "get_risk_settings_async",
    "update_risk_settings", "get_engine", "engines_running", "sub_engines",
    "ENGINE_NAMES", "IMPLEMENTED_NAMES", "control_target", "effective_settings",
    "set_engine_running", "set_ai_eval", "AI_EVAL_KEYS",
    "RemoteControlFailed", "place_market_order",
]


def get_risk_settings() -> dict:
    return _risk.get()


async def get_risk_settings_async() -> dict:
    return await _risk.get_async()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


# ── Engine lifecycle (restructure phase1/010) ────────────────────────────────
# Named operations, not re-exported singletons, so no page loops over engines
# choosing lifecycle again. The table and the BULK loops are in
# services/engines/registry.py and deliberately not re-exported: their only
# caller is the handover, a service, which may not import a controller.


# Which engines exist, in the fixed binding order. Re-exported so a router can
# render the tab from it rather than restating the list and drifting.
ENGINE_NAMES = _engines.ENGINE_NAMES
# Not the same list: ENGINE_NAMES is the sync protocol's positional order and
# still holds Bounce's empty slot. See services/engines/registry.py.
IMPLEMENTED_NAMES = _engines.IMPLEMENTED_NAMES


def get_engine(name: str) -> Any:
    """The named engine's live instance (its panel needs status attributes
    and its refresh-callback hook)."""
    return _engines.instance(name)


def engines_running() -> dict:
    return _engines.running()


def sub_engines() -> tuple:
    """(breakout, bounce, reversal) instances in the fixed binding order the
    sync server's server_start has always received them."""
    return _engines.all_instances()


# ── Acting on the node that is actually trading ──────────────────────────────
# With the VPS trading, this machine's engines are stood down, so a Start/Stop
# here "does nothing useful while looking like it worked" (the sync server's
# own words). remote_control.py decides where a control lands.

AI_EVAL_KEYS = _remote.AI_EVAL_KEYS
RemoteControlFailed = _remote.RemoteControlFailed


def control_target() -> str:
    """"local", "remote" or "centralized" -- which node a control will reach."""
    return _remote.where()


def effective_settings(local: dict) -> dict:
    """The settings the engines are actually obeying."""
    return _remote.effective_settings(local)


async def set_engine_running(*args, **kwargs) -> dict:
    return await _remote.set_engine_running(*args, **kwargs)


async def set_ai_eval(*args, **kwargs) -> dict:
    return await _remote.set_ai_eval(*args, **kwargs)


async def place_market_order(*args, **kwargs) -> dict:
    """Place a market order on whichever node is actually trading."""
    return await _remote.place_market_order(*args, **kwargs)
