"""MT5 accounts over the node link: the Mac's credentials and its demo/live
choice reach the VPS, which switches to match (owner, 2026-09-26).

The Mac is the source of truth. It sends login, password and server for each
account it has complete, plus which account it is on, whenever it connects and
whenever it saves credentials or switches. Never the terminal path: that is
per machine. The VPS stores them; if the Mac is on the other environment it
runs the same guarded `environment.switch` the Mac does and restarts; if it is
on the same one, it logs its running bridge into the account.

Credentials, so the settings sync (which never carries them) is not used. The
link is TLS with a pinned certificate and a shared token (sync/tls_util.py).
Pinned by tests/core/test_mt5_accounts_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging

from backend.src.services.broker import credentials_repo as _creds
from backend.src.services.broker import environment as _env
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_MT5_ACCOUNTS, MSG_MT5_ACCOUNTS_ACK, make,
)

log = logging.getLogger(__name__)

_FIELDS = {
    "demo": ("login", "password_enc", "server"),
    "live": ("live_login", "live_password_enc", "live_server"),
}


def accounts_snapshot() -> dict:
    """What the Mac sends: each complete account, and the one it is on."""
    creds = _creds.get_mt5_credentials() or {}
    accounts = {}
    for env, (login, password, server) in _FIELDS.items():
        if creds.get(login) and creds.get(password) and creds.get(server):
            accounts[env] = {"login": str(creds[login]), "password": creds[password],
                             "server": creds[server]}
    return {"accounts": accounts, "environment": _env.current()}


async def apply_accounts(msg: dict, bridge, *, save=None, current=None, switch=None,
                         write_bridge_file=None, align=None) -> dict:
    """The VPS's side. Returns {"switched", "environment", "error"}.

    Stores first: `environment.switch` refuses an account with no saved
    credentials, and the ones that just arrived must be there when it looks.
    """
    save = save or _creds.save_mt5_credentials
    current = current or _env.current
    switch = switch or _env.switch
    write_bridge_file = write_bridge_file or _creds.sync_bridge_credentials_file
    align = align or _env.align_bridge

    accounts = msg.get("accounts") or {}
    updates: dict = {}
    for env, (login, password, server) in _FIELDS.items():
        a = accounts.get(env) or {}
        if a.get("login") and a.get("password") and a.get("server"):
            lg = str(a["login"]).strip()
            updates.update({login: int(lg) if lg.isdigit() else lg,
                            password: a["password"], server: a["server"]})
    before = current()
    if updates:
        save(updates)

    target = msg.get("environment")
    if target in _FIELDS and target != before:
        try:
            switch(target)
        except ValueError as e:
            return {"switched": False, "environment": before, "error": str(e)}
        return {"switched": True, "environment": target, "error": ""}

    if before in accounts and any(k in updates for k in _FIELDS[before]):
        write_bridge_file(before)
        if bridge is not None:
            await align(bridge)
    return {"switched": False, "environment": before, "error": ""}


async def push_from_this_node() -> None:
    """Send this machine's accounts to its VPS now, if it is paired and
    connected. A no-op on the VPS itself and on an unpaired machine."""
    from backend.src.services.cluster.sync import client as _client
    cli = _client.get_instance()
    if cli is not None:
        await cli.push_mt5_accounts()


async def _alert(text: str) -> None:
    from backend.src.services.telegram import alerts as telegram_alerts
    try:
        await telegram_alerts.send_message(text, event_type="sync_liveness")
    except Exception as e:
        log.warning("[MT5Accounts] could not send the alert: %s", e)


class ClientMt5AccountsMixin:
    async def push_mt5_accounts(self) -> None:
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            return
        try:
            await self._ws.send(json.dumps(make(MSG_MT5_ACCOUNTS, **accounts_snapshot())))
        except Exception as e:
            log.debug("[SyncClient] MT5 accounts send failed (resent on reconnect): %s", e)

    async def _on_mt5_accounts_ack(self, msg: dict) -> None:
        if msg.get("error"):
            log.error("[SyncClient] the VPS did not take this machine's MT5 account: %s",
                      msg["error"])
            await _alert("*The VPS is not on this machine's MT5 account*\n"
                         f"It stayed on {msg.get('environment')}: {msg['error']}")
        elif msg.get("switched"):
            log.warning("[SyncClient] the VPS switched to %s to match this machine",
                        msg.get("environment"))


class ServerMt5AccountsMixin:
    async def _handle_mt5_accounts(self, ws, msg: dict) -> None:
        bridge = getattr(getattr(self, "_main_engine", None), "_bridge", None)
        try:
            result = await apply_accounts(msg, bridge)
        except Exception as e:
            log.warning("[SyncServer] applying the Mac's MT5 accounts failed: %s", e)
            result = {"switched": False, "environment": _env.current(), "error": str(e)}
        await ws.send(json.dumps(make(MSG_MT5_ACCOUNTS_ACK, **result)))
        if result["switched"]:
            env = result["environment"].upper()
            log.warning("[SyncServer] switched to %s to match the Mac; restarting", env)
            await _alert(f"*VPS switched to {env}* to match the Mac. Restarting now.")
            from backend.src.utils.os_utils import repo_root, restart_app
            asyncio.get_running_loop().call_later(2, restart_app, repo_root())
