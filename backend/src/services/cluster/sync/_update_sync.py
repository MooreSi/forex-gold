"""Upgrade the VPS from the Mac, and compare the two nodes' commits
(owner, 2026-09-27).

The VPS updates exactly as its own Settings > Update button does, through
`core_app_update.apply_update()`: fetch + force-checkout origin/main, pip
install, pycache sweep, EA deploy, then restart. This adds a way to ASK, not
a second updater.

The VPS answers at once (MSG_UPDATE_NODE_ACK): an update takes minutes and
ends in a restart that drops the link, so the Mac cannot wait for the end.
If it fails before restarting, the VPS says so afterwards
(MSG_UPDATE_NODE_RESULT) and the Mac keeps it for the screen. One at a time:
a second request while one runs is refused, the lesson of the two
MSG_GIT_UPDATEs 3 s apart on 2026-09-26.

The heartbeat carries the VPS's commit and git version so the Mac can say
whether both nodes run the same code. A VPS that sends no commit (older
build) is "unknown", never "out of sync".

Pinned by tests/core/test_update_vps_over_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_UPDATE_NODE, MSG_UPDATE_NODE_ACK, MSG_UPDATE_NODE_RESULT, make,
)
from backend.src.services.positions import core_app_update

log = logging.getLogger(__name__)

_updating = False


def heartbeat_fields() -> dict:
    """This node's commit and git version, for the status heartbeat. Never
    raises: the heartbeat carries balances and positions too."""
    try:
        commit = core_app_update.get_local_commit_sha(short=False)
        git_version = core_app_update.get_git_version()
    except Exception as e:
        log.debug("[Sync] commit unreadable for heartbeat: %s", e)
        commit, git_version = "", ""
    return {"commit": commit or "", "git_version": git_version or ""}


def _local() -> dict:
    return heartbeat_fields()


def version_report(remote_status: dict) -> dict:
    """{local, remote, in_sync}. `remote` is None and `in_sync` None when the
    VPS has not told us its commit (no link, or an older VPS)."""
    local = _local()
    remote_commit = (remote_status or {}).get("commit")
    if not remote_commit:
        return {"local": local, "remote": None, "in_sync": None}
    remote = {"commit": remote_commit, "git_version": remote_status.get("git_version") or ""}
    in_sync: Optional[bool] = (local["commit"] == remote_commit) if local["commit"] else None
    return {"local": local, "remote": remote, "in_sync": in_sync}


class ClientUpdateMixin:
    last_update_result: Optional[dict] = None

    async def request_peer_update(self, timeout: float = 10.0) -> dict:
        """Ask the VPS to update. Returns its immediate {"ok", "note"}."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        event = self.__dict__.setdefault("_update_ack_event", asyncio.Event())
        event.clear()
        self.last_update_result = None
        await self._ws.send(json.dumps(make(MSG_UPDATE_NODE)))
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return self.__dict__.get("_last_update_ack", {})

    def _on_update_ack(self, msg: dict) -> None:
        self._last_update_ack = msg
        self.__dict__.setdefault("_update_ack_event", asyncio.Event()).set()

    def _on_update_result(self, msg: dict) -> None:
        self.last_update_result = {"ok": bool(msg.get("ok")), "note": str(msg.get("note") or "")}
        log.error("[SyncClient] the VPS update failed: %s", self.last_update_result["note"])


class ServerUpdateMixin:
    async def _handle_update_node(self, ws, msg: dict) -> None:
        global _updating
        if _updating:
            await ws.send(json.dumps(make(MSG_UPDATE_NODE_ACK, ok=False,
                                          note="An update is already running on the VPS.")))
            return
        _updating = True
        await ws.send(json.dumps(make(
            MSG_UPDATE_NODE_ACK, ok=True,
            note="The VPS is updating to the latest commit and will restart when done.")))
        asyncio.ensure_future(self._run_update(ws))

    async def _run_update(self, ws) -> None:
        global _updating
        log.warning("[SyncServer] updating at the Mac's request")
        try:
            result = await core_app_update.apply_update(restart=True)
        except Exception as e:
            result = {"ok": False, "error": f"Update failed: {e}"}
        finally:
            _updating = False
        if not result.get("ok"):
            note = str(result.get("error") or "Update failed.")
            log.error("[SyncServer] the Mac's update request failed: %s", note)
            try:
                await ws.send(json.dumps(make(MSG_UPDATE_NODE_RESULT, ok=False, note=note)))
            except Exception as e:
                log.debug("[SyncServer] could not report the failed update: %s", e)


def current_version_report() -> dict:
    """The Remote tab's read: both nodes' commits, whether they match, and a
    failed update the VPS reported, if any."""
    from backend.src.services.cluster.sync import client as _client
    cli = _client.get_instance()
    connected = cli is not None and getattr(cli, "conn_state", None) == CONN_CONNECTED
    status = (getattr(cli, "remote_status", None) or {}) if connected else {}
    report = version_report(status)
    # Why the VPS's commit is missing, when it is: a connected VPS whose
    # heartbeat has no commit runs a build from before e613be5 and must be
    # updated once by other means; no link is a different fix (2026-09-28).
    if not connected:
        reason = "not_connected"
    elif report.get("remote") and report["remote"].get("commit"):
        reason = "reported"
    else:
        reason = "older_build"
    return {**report, "remote_reason": reason,
            "last_update": getattr(cli, "last_update_result", None) if cli else None}
