"""Which node a Dashboard report is read from: the one that trades.

Fill cost and GEX are summaries of tables in the trader's own database. When
the VPS is the active trader, this machine's copies describe a node that places
no orders, so the Mac asks the VPS for the same report (sync/_peer_report_sync.py)
and labels the answer `node: "remote"`.

**No fallback to this node's own data.** If the VPS is trading and cannot be
reached, showing the Mac's fills would be the misreport this module exists to
end, so the failure is raised and the card says so.

Read-only. Nothing here places, closes or sizes a trade.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.src.db import database as db_module
from backend.src.services.broker import fill_cost_report
from backend.src.services.cluster.remote_control import RemoteControlFailed
from backend.src.services.cluster.sync import client as _client
from backend.src.services.cluster.sync import server as _server
from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
from backend.src.services.market import gex_report

log = logging.getLogger(__name__)

__all__ = ["REPORTS", "run_local", "fill_cost_async", "gex_async"]

# The only reports a peer may ask for. The name arrives off the wire.
REPORTS = {
    "fill_cost": fill_cost_report.report_async,
    "gex": gex_report.report_async,
}
# Per report, the arguments it takes and how each is checked.
_ARGS = {
    "fill_cost": {"days": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 365},
    "gex": {},
}


def trader_is_peer() -> bool:
    """True on a paired Mac whose active trader is the VPS.

    The VPS itself is never its own peer, and an install with no VPS configured
    is its own trader whatever the stored setting says.
    """
    try:
        if _server.get_instance() is not None:
            return False
        host, _port, _token = _client.SyncClient.load_config()
        return bool(host) and db_module.get_active_trader() == TRADER_REMOTE_VPS
    except Exception:
        return False


async def run_local(name: Any, args: dict) -> Any:
    """Run a named report on this node. Refuses a name or argument not in the tables."""
    if name not in REPORTS:
        raise ValueError(f"unknown report {name!r}")
    checks = _ARGS.get(name, {})
    for key, value in args.items():
        if key not in checks or not checks[key](value):
            raise ValueError(f"report {name!r} does not take {key}={value!r}")
    return await REPORTS[name](**args)


async def on_trading_node(name: str, **args: Any) -> dict:
    """The named report from the node that trades, tagged with which node that was."""
    if not trader_is_peer():
        return {**await REPORTS[name](**args), "node": "local"}
    try:
        ack = await _client.get_instance().request_peer_report(name, args)
    except asyncio.TimeoutError as exc:
        log.warning("[peer_reports] %s: no answer from the trading node", name)
        raise RemoteControlFailed(
            "The trading node did not answer in time. If this keeps happening it "
            "may be running older code that cannot send this report."
        ) from exc
    except Exception as exc:
        log.warning("[peer_reports] %s did not reach the trading node: %s", name, exc)
        raise RemoteControlFailed(
            f"The trading node could not be reached ({exc})."
        ) from exc
    if (ack or {}).get("error"):
        raise RemoteControlFailed(f"The trading node refused: {ack['error']}")
    return {**((ack or {}).get("result") or {}), "node": "remote"}


async def fill_cost_async(days: int = 14) -> dict:
    return await on_trading_node("fill_cost", days=days)


async def gex_async() -> dict:
    return await on_trading_node("gex")
