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
import subprocess
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


# The commit this process is running: the checkout as it was on the first
# heartbeat after boot. `heartbeat_fields()["commit"]` is read from disk on
# every beat, so a VPS that pulled and never restarted reported the new commit
# while running the old code, and the Mac said "in sync" (2026-09-28).
_running_commit: Optional[str] = None


def running_commit() -> str:
    """The commit read on the first heartbeat, kept for the life of the
    process. "" if it could not be read; that is not cached, so a git hiccup
    on the first beat does not blank it for good."""
    global _running_commit
    if _running_commit:
        return _running_commit
    try:
        sha = core_app_update.get_local_commit_sha(short=False) or ""
    except Exception as e:
        log.debug("[Sync] running commit unreadable: %s", e)
        return ""
    if sha:
        _running_commit = sha
    return sha


def _local() -> dict:
    return heartbeat_fields()


def _origin_commit() -> str:
    """The commit this checkout last saw on origin/main (no fetch), or "".
    Upgrade VPS pulls origin/main, so when this machine runs commits it has
    not pushed, the VPS can only ever land here (2026-09-29)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"origin/{core_app_update._BRANCH}"],
            cwd=str(core_app_update._REPO_ROOT), capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        log.debug("[Sync] origin commit unreadable: %s", e)
        return ""
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    return sha if core_app_update._is_sha(sha) else ""


def version_report(remote_status: dict, origin_commit: str = "") -> dict:
    """{local, remote, in_sync, restart_pending, origin_commit}. `remote` is None and
    `in_sync` None when the VPS has not told us its commit (no link, or an
    older VPS).

    The VPS's commit is the one it is RUNNING when it says so
    (`running_commit`), so a VPS that pulled and never restarted is not in
    sync -- and Upgrade VPS, which pulls and restarts, is what fixes it.
    `restart_pending` is True in exactly that case, None from a VPS that does
    not send `running_commit`. `origin_commit` is what Upgrade VPS would
    pull, so the screen can tell "the VPS is behind" from "this machine has
    commits nobody pushed"."""
    local = _local()
    status = remote_status or {}
    checked_out = status.get("commit")
    running = status.get("running_commit")
    remote_commit = running or checked_out
    if not remote_commit:
        return {"local": local, "remote": None, "in_sync": None, "restart_pending": None,
                "origin_commit": origin_commit}
    remote = {"commit": remote_commit, "git_version": status.get("git_version") or ""}
    in_sync: Optional[bool] = (local["commit"] == remote_commit) if local["commit"] else None
    restart_pending: Optional[bool] = (
        (running != checked_out) if (running and checked_out) else None)
    return {"local": local, "remote": remote, "in_sync": in_sync,
            "restart_pending": restart_pending, "origin_commit": origin_commit}


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
    report = version_report(status, origin_commit=_origin_commit())
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
