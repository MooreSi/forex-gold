"""
Remote administration WebSocket server.

Runs on the admin's machine (fixed IP 217.155.25.160, port 8443).
Accepts authenticated connections from remote clients.

Startup:
    import forex_trader.remote.server as remote_server
    remote_server.start()          # call once during app startup
    remote_server.stop()           # call on shutdown
"""

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Optional

from forex_trader.config import USER_DATA_DIR
from forex_trader.remote.protocol import (
    MSG_HELLO, MSG_PONG, MSG_STATUS, MSG_DIAGNOSTICS, MSG_UPDATE_STATUS,
    MSG_REGISTER, MSG_WELCOME, MSG_REJECT, MSG_REVOKE, MSG_LICENCE,
    MSG_PING, MSG_GET_DIAG, MSG_GIT_UPDATE, MSG_VERSION_INFO,
    MSG_ADMIN_HELLO, MSG_ADMIN_APPROVE, MSG_ADMIN_REJECT, MSG_ADMIN_ISSUE,
    MSG_ADMIN_REVOKE, MSG_ADMIN_DB_OP, MSG_ADMIN_WELCOME, MSG_PENDING_PUSH,
    MSG_CLIENTS_PUSH, MSG_LICENCES_PUSH, MSG_ADMIN_RESULT,
    make,
)
from forex_trader.remote.tls import server_ssl_context, SERVER_PORT

log = logging.getLogger(__name__)

_REMOTE_DIR         = Path(USER_DATA_DIR) / "remote"
_TOKENS_FILE        = _REMOTE_DIR / "allowed_tokens.json"
_PENDING_FILE       = _REMOTE_DIR / "pending_registrations.json"
_REVOKED_FILE       = _REMOTE_DIR / "revoked_tokens.json"
_ADMIN_MACHINES_FILE = _REMOTE_DIR / "admin_machines.json"
_CHANGELOG_FILE     = Path(__file__).parent.parent / "CHANGELOG.md"

# ── In-memory state ─────────────────────────────────────────────────────────

# {token: {"name": str, "registered_at": float, "added_by": "admin"}}
_allowed_tokens: dict[str, dict] = {}

# {token: {"name": str, "platform": str, "version": str, "ts": float}}
_pending: dict[str, dict] = {}

# Tokens explicitly revoked by admin — these receive reason="revoked" on
# reconnect so the client knows to delete its local token file and restart
# rather than re-registering with the same UUID.
_revoked_tokens: set = set()

# {token: {"ws": websocket, "info": dict, "last_seen": float, "diagnostics": dict}}
_connected: dict[str, dict] = {}

# ── Remote admin machines ────────────────────────────────────────────────────
# List of IOKit Platform UUIDs that are allowed to open an elevated admin
# connection to this server.  Managed via add/remove_admin_machine().
# [{"uuid": str, "label": str, "added_at": float}]
_admin_machines: list[dict] = []

# Connected admin clients — keyed by machine UUID.
# {uuid: {"ws": websocket}}
_admin_clients: dict[str, dict] = {}

# Optional callbacks registered by the KeyGen admin panel on the main Mac so
# the server can read/write licences.db without importing KeyGen directly.
# _kg_get_all_fn() → list[dict]  (all licence records)
# _kg_insert_fn(record: dict)    (insert a new licence record)
# _kg_revoke_fn(id: int) → bool  (revoke a licence by DB id)
# _kg_reinstate_fn(id: int) → bool
# _kg_delete_fn(id: int) → bool
# _kg_sign_fn(machine_id: str, expiry_date: str) → str  (Ed25519-sign a licence
#   key). The private signing key lives only in KeyGen/licence_signing.py —
#   never imported directly into this (public, shipped-to-every-client)
#   module, so this callback is the only way approve_registration() can ever
#   issue a licence key.
_kg_get_all_fn    = None
_kg_insert_fn     = None
_kg_revoke_fn     = None
_kg_reinstate_fn  = None
_kg_delete_fn     = None
_kg_sign_fn       = None

_server_task: Optional[asyncio.Task] = None
_server_obj = None

# Rate-limiting: {ip: [timestamp, ...]}
_auth_failures: dict[str, list[float]] = {}
_MAX_FAILURES  = 5
_FAILURE_WINDOW = 600  # 10 minutes


# ── Token persistence ────────────────────────────────────────────────────────

def _load_tokens() -> None:
    global _allowed_tokens, _pending, _revoked_tokens
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    if _TOKENS_FILE.exists():
        try:
            _allowed_tokens = json.loads(_TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _allowed_tokens = {}
    if _PENDING_FILE.exists():
        try:
            _pending = json.loads(_PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            _pending = {}
    if _REVOKED_FILE.exists():
        try:
            _revoked_tokens = set(json.loads(_REVOKED_FILE.read_text(encoding="utf-8")))
        except Exception:
            _revoked_tokens = set()


def _save_tokens() -> None:
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    _TOKENS_FILE.write_text(json.dumps(_allowed_tokens, indent=2), encoding="utf-8")


def _save_pending() -> None:
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    _PENDING_FILE.write_text(json.dumps(_pending, indent=2), encoding="utf-8")


def _save_revoked() -> None:
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    _REVOKED_FILE.write_text(json.dumps(list(_revoked_tokens)), encoding="utf-8")


# ── Admin machine management ─────────────────────────────────────────────────

def _load_admin_machines() -> None:
    global _admin_machines
    if _ADMIN_MACHINES_FILE.exists():
        try:
            _admin_machines = json.loads(_ADMIN_MACHINES_FILE.read_text(encoding="utf-8"))
        except Exception:
            _admin_machines = []


def _save_admin_machines() -> None:
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    _ADMIN_MACHINES_FILE.write_text(json.dumps(_admin_machines, indent=2), encoding="utf-8")


def add_admin_machine(uuid: str, label: str = "") -> None:
    """Grant remote admin access to a machine identified by its IOKit UUID."""
    if not any(m["uuid"] == uuid for m in _admin_machines):
        _admin_machines.append({"uuid": uuid, "label": label or uuid[:8], "added_at": time.time()})
        _save_admin_machines()
        log.info("[RemoteServer] Admin machine added: %s (%s)", label or uuid[:8], uuid)


def remove_admin_machine(uuid: str) -> None:
    """Revoke remote admin access for a machine UUID."""
    before = len(_admin_machines)
    _admin_machines[:] = [m for m in _admin_machines if m["uuid"] != uuid]
    if len(_admin_machines) < before:
        _save_admin_machines()
        # Disconnect any live admin session for this UUID.
        entry = _admin_clients.get(uuid)
        if entry:
            asyncio.create_task(_close_ws(entry["ws"]))
        log.info("[RemoteServer] Admin machine removed: %s", uuid)


def get_admin_machines() -> list[dict]:
    return list(_admin_machines)


def is_admin_machine_uuid(uuid: str) -> bool:
    return bool(uuid) and any(m["uuid"] == uuid for m in _admin_machines)


def register_kg_callbacks(get_all_fn, insert_fn, revoke_fn=None,
                          reinstate_fn=None, delete_fn=None, sign_fn=None) -> None:
    """Register callbacks for KeyGen licence DB access and licence signing.

    Called by forex_admin.py on the main Mac so the server can serve and
    persist licence records for remote admin clients, and sign new licence
    keys without ever importing the private key module (KeyGen/licence_
    signing.py) into this shipped-everywhere package.
    """
    global _kg_get_all_fn, _kg_insert_fn, _kg_revoke_fn, _kg_reinstate_fn, _kg_delete_fn, _kg_sign_fn
    _kg_get_all_fn   = get_all_fn
    _kg_insert_fn    = insert_fn
    _kg_revoke_fn    = revoke_fn
    _kg_reinstate_fn = reinstate_fn
    _kg_delete_fn    = delete_fn
    _kg_sign_fn      = sign_fn
    log.debug("[RemoteServer] KeyGen DB callbacks registered")


# ── Push helpers for admin clients ────────────────────────────────────────────

async def _push_state_to_admin(ws) -> None:
    """Send pending registrations, client list, and licences to one admin WS."""
    try:
        await ws.send(json.dumps(make(MSG_PENDING_PUSH, items=list(_pending.values()),
                                     tokens=list(_pending.keys()))))
    except Exception:
        return
    try:
        await ws.send(json.dumps(make(MSG_CLIENTS_PUSH, items=get_all_clients())))
    except Exception:
        return
    if _kg_get_all_fn:
        try:
            licences = _kg_get_all_fn()
            await ws.send(json.dumps(make(MSG_LICENCES_PUSH, items=licences)))
        except Exception:
            pass


# Duration options offered on the Telegram Approve buttons — same labels and
# day counts as KeyGen/forex_admin.py's _SUB_TYPES / approve_registration()'s
# sub_days mapping, just given short codes so they fit in a callback_data
# string (Telegram's 64-byte cap; a full token is already 64 hex chars, far
# too long, hence addressing pending requests by their 8-char token prefix
# instead — same "short handle, not the real ID" idea as the panel's
# per-channel slugs, see core_bot_panel.py's module docstring).
_REG_DURATIONS = [
    ("6m",   "6 Months"),
    ("1y",   "1 Year"),
    ("2y",   "2 Years"),
    ("3y",   "3 Years"),
    ("perp", "Perpetual"),
]


async def _notify_new_registration(hostname: str, email: str, nickname: str,
                                    ip: str, token: str = "") -> None:
    """Send a Telegram alert to the admin when a new licence/registration
    request comes in, with inline Approve (per duration)/Reject buttons."""
    try:
        from forex_trader.core import telegram_alerts
        msg = (
            "New registration request\n"
            f"Name: {nickname or '—'}\n"
            f"Email: {email or '—'}\n"
            f"Hostname: {hostname or '—'}\n"
            f"IP: {ip or '—'}"
        )
        reply_markup = None
        if token:
            from forex_trader.core.core_bot_panel import _btn
            short = token[:8]
            approve_row_1 = [_btn(f"✅ {lbl}", "reg_ap", short, code)
                              for code, lbl in _REG_DURATIONS[:3]]
            approve_row_2 = [_btn(f"✅ {lbl}", "reg_ap", short, code)
                              for code, lbl in _REG_DURATIONS[3:]]
            reject_row = [_btn("❌ Reject", "reg_rj", short)]
            reply_markup = {"inline_keyboard": [approve_row_1, approve_row_2, reject_row]}
        await telegram_alerts.send_message(msg, reply_markup=reply_markup)
    except Exception as e:
        log.warning("[RemoteServer] Registration Telegram notify failed: %s", e)


async def _push_pending_to_all_admins() -> None:
    """Broadcast updated pending list to every connected admin client."""
    if not _admin_clients:
        return
    payload = json.dumps(make(MSG_PENDING_PUSH, items=list(_pending.values()),
                              tokens=list(_pending.keys())))
    dead = []
    for uuid, entry in list(_admin_clients.items()):
        try:
            await entry["ws"].send(payload)
        except Exception:
            dead.append(uuid)
    for u in dead:
        _admin_clients.pop(u, None)


async def _push_clients_to_all_admins() -> None:
    """Broadcast updated client list to every connected admin client."""
    if not _admin_clients:
        return
    payload = json.dumps(make(MSG_CLIENTS_PUSH, items=get_all_clients()))
    dead = []
    for uuid, entry in list(_admin_clients.items()):
        try:
            await entry["ws"].send(payload)
        except Exception:
            dead.append(uuid)
    for u in dead:
        _admin_clients.pop(u, None)


async def _push_licences_to_all_admins() -> None:
    if not _admin_clients or not _kg_get_all_fn:
        return
    try:
        licences = _kg_get_all_fn()
    except Exception:
        return
    payload = json.dumps(make(MSG_LICENCES_PUSH, items=licences))
    dead = []
    for uuid, entry in list(_admin_clients.items()):
        try:
            await entry["ws"].send(payload)
        except Exception:
            dead.append(uuid)
    for u in dead:
        _admin_clients.pop(u, None)


# ── Admin command handler ─────────────────────────────────────────────────────

async def _handle_admin_command(m: dict, admin_uuid: str) -> None:
    ws = _admin_clients.get(admin_uuid, {}).get("ws")
    if not ws:
        return
    t = m.get("type")

    async def _result(ok: bool, msg: str = "") -> None:
        try:
            await ws.send(json.dumps(make(MSG_ADMIN_RESULT, ok=ok, msg=msg,
                                          cmd=t)))
        except Exception:
            pass

    if t == MSG_ADMIN_APPROVE:
        token   = m.get("token", "")
        name    = m.get("name", "")
        sub     = m.get("subscription_type", "Perpetual")
        if approve_registration(token, name, sub):
            # Also write to licences.db if callback registered
            if _kg_insert_fn:
                try:
                    _kg_insert_fn(_make_kg_record(token))
                except Exception as ex:
                    log.warning("[RemoteServer] kg_insert_fn failed: %s", ex)
            await _result(True, f"Approved {name}")
            await _push_pending_to_all_admins()
            await _push_clients_to_all_admins()
            await _push_licences_to_all_admins()
        else:
            await _result(False, "Token not in pending list")

    elif t == MSG_ADMIN_REJECT:
        token = m.get("token", "")
        if token in _pending:
            _pending.pop(token, None)
            _save_pending()
            log.info("[RemoteServer] Pending registration rejected by remote admin: %s", token[:8])
            await _result(True, "Rejected")
            await _push_pending_to_all_admins()
        else:
            await _result(False, "Token not found")

    elif t == MSG_ADMIN_REVOKE:
        token = m.get("token", "")
        revoke_token(token)
        await _result(True, "Revoked")
        await _push_clients_to_all_admins()
        await _push_licences_to_all_admins()

    elif t == MSG_ADMIN_ISSUE:
        # Remote admin issues a new licence — relay to licences.db if available.
        record = m.get("record", {})
        if not record:
            await _result(False, "No record provided")
            return
        if _kg_insert_fn:
            try:
                _kg_insert_fn(record)
                log.info("[RemoteServer] Licence issued via remote admin for %s",
                         record.get("email", "?"))
                await _result(True, "Licence issued")
                await _push_licences_to_all_admins()
            except Exception as ex:
                log.warning("[RemoteServer] Remote licence insert failed: %s", ex)
                await _result(False, str(ex))
        else:
            log.warning("[RemoteServer] kg_insert_fn not registered — licence not persisted to DB")
            await _result(False, "Licence DB unavailable on server — open the main admin panel first")

    elif t == MSG_ADMIN_DB_OP:
        op  = m.get("op", "")
        lid = m.get("id", 0)
        fn_map = {"revoke": _kg_revoke_fn, "reinstate": _kg_reinstate_fn,
                  "delete": _kg_delete_fn}
        fn = fn_map.get(op)
        if not fn:
            await _result(False, f"Unknown DB op or DB unavailable: {op}")
            return
        try:
            ok2 = bool(fn(lid))
            labels = {"revoke": ("Revoked", "Could not revoke"),
                      "reinstate": ("Reinstated", "Could not reinstate"),
                      "delete": ("Deleted", "Could not delete")}
            ok_msg, fail_msg = labels.get(op, ("Done", "Failed"))
            await _result(ok2, ok_msg if ok2 else fail_msg)
            if ok2:
                await _push_licences_to_all_admins()
        except Exception as ex:
            await _result(False, str(ex))


def _make_kg_record(token: str) -> dict:
    """Build a licences.db insert record from an approved token."""
    import json as _json
    meta = _allowed_tokens.get(token, {})
    pending = {}  # already removed from _pending at this point
    return {
        "email":           meta.get("email", ""),
        "registration_id": meta.get("machine_id", ""),
        "machine_model":   "",
        "machine_serial":  "",
        "hostname":        meta.get("hostname", ""),
        "macos_version":   "",
        "cpu":             "",
        "memory":          "",
        "licence_key":     meta.get("licence_key", ""),
        "expiry_date":     meta.get("expiry_date", "perpetual"),
        "licence_type":    meta.get("subscription_type", "Perpetual"),
        "nickname":        meta.get("nickname", ""),
        "notes":           "Auto-issued via remote admin approval",
        "raw_json":        "",
    }


# ── Admin WebSocket handler ───────────────────────────────────────────────────

async def _admin_handler(websocket, uuid: str) -> None:
    """Elevated session for a trusted remote admin machine."""
    _admin_clients[uuid] = {"ws": websocket}
    label = next((m["label"] for m in _admin_machines if m["uuid"] == uuid), uuid[:8])
    log.info("[RemoteServer] Remote admin connected: %s", label)
    try:
        await _push_state_to_admin(websocket)
        async for raw in websocket:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            await _handle_admin_command(m, uuid)
    except Exception:
        pass
    finally:
        _admin_clients.pop(uuid, None)
        log.info("[RemoteServer] Remote admin disconnected: %s", label)


def approve_registration(token: str, display_name: str, subscription_type: str = "Perpetual") -> bool:
    """Approve a pending client registration.  Returns True on success."""
    from datetime import datetime, timedelta
    if token not in _pending:
        return False
    sub_days = {"6 Months": 183, "1 Year": 365, "2 Years": 730, "3 Years": 1095}
    days = sub_days.get(subscription_type, 0)
    expiry_date = (
        (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        if days > 0 else "perpetual"
    )
    pending = _pending[token]
    machine_id = pending.get("machine_id", "")
    email      = pending.get("email", "")
    nickname   = pending.get("nickname", "")

    # Sign the licence key via the KeyGen-registered callback — the private
    # Ed25519 key never lives in this module (see register_kg_callbacks).
    licence_key = ""
    if machine_id:
        if _kg_sign_fn:
            try:
                licence_key = _kg_sign_fn(machine_id, expiry_date)
            except Exception as exc:
                log.error("[RemoteServer] kg_sign_fn failed for %s: %s", token[:8], exc)
        else:
            log.error(
                "[RemoteServer] kg_sign_fn not registered — approving %s with no licence key",
                token[:8],
            )

    _allowed_tokens[token] = {
        "name":              display_name or pending.get("hostname", token[:8]),
        "registered_at":     time.time(),
        "email":             email,
        "nickname":          nickname,
        "platform":          pending.get("platform", "unknown"),
        "hostname":          pending.get("hostname", "?"),
        "machine_id":        machine_id,
        "subscription_type": subscription_type,
        "expiry_date":       expiry_date,
        "licence_key":       licence_key,
    }
    _pending.pop(token, None)
    _revoked_tokens.discard(token)   # re-approval lifts a prior revocation
    _save_tokens()
    _save_pending()
    _save_revoked()
    # Clear the IP's auth-failure count so the newly approved client can
    # connect and receive updates without hitting the rate limiter.
    client_ip = pending.get("ip", "")
    if client_ip:
        _auth_failures.pop(client_ip, None)
    # If the client is currently connected, deliver the licence immediately.
    if licence_key:
        entry = _connected.get(token)
        if entry:
            asyncio.create_task(_send_licence(entry["ws"], token))
    log.info("[RemoteServer] Approved %s — licence generated, expiry=%s", token[:8], expiry_date)
    return True


def resign_all_licences() -> dict:
    """Re-sign every approved token's licence key with the currently
    registered sign_fn, pushing the refreshed key to anyone connected now.

    Needed because switching the signing scheme (e.g. the HMAC keygen.py ->
    Ed25519 migration) instantly invalidates every already-issued key against
    the new verify.py — guard.py's enforce() runs before the remote-client
    connection even starts, so an already-stuck client can't be reached by
    any push at all; the only way to avoid stranding every existing customer
    is to have the currently-connected ones self-heal automatically and the
    rest pick up the corrected key the moment they next reconnect.

    Ed25519 signing is deterministic (unlike the old HMAC scheme, this
    produces identical bytes for identical inputs), so calling this
    unconditionally on every admin-console startup is always safe: a key
    already signed under the current scheme re-signs to the exact same
    string (no-op, no re-push), while one signed under a retired scheme
    changes and gets pushed. Returns {"resigned": N, "unchanged": N,
    "skipped": N}.
    """
    if not _kg_sign_fn:
        return {"resigned": 0, "unchanged": 0, "skipped": len(_allowed_tokens)}
    resigned = unchanged = skipped = 0
    for token, meta in _allowed_tokens.items():
        machine_id = meta.get("machine_id", "")
        if not machine_id:
            skipped += 1
            continue
        try:
            new_key = _kg_sign_fn(machine_id, meta.get("expiry_date", ""))
        except Exception as exc:
            log.warning("[RemoteServer] resign failed for %s: %s", token[:8], exc)
            skipped += 1
            continue
        if new_key == meta.get("licence_key"):
            unchanged += 1
            continue
        meta["licence_key"] = new_key
        resigned += 1
        entry = _connected.get(token)
        if entry:
            asyncio.create_task(_send_licence(entry["ws"], token))
    if resigned:
        _save_tokens()
        log.info("[RemoteServer] Re-signed %d licence(s) with the current signing key "
                  "(%d already current, %d skipped)", resigned, unchanged, skipped)
    return {"resigned": resigned, "unchanged": unchanged, "skipped": skipped}


async def _send_licence(ws, token: str) -> None:
    """Push MSG_LICENCE to a connected client."""
    tok_meta = _allowed_tokens.get(token, {})
    try:
        await ws.send(json.dumps(make(
            MSG_LICENCE,
            licence_key=tok_meta.get("licence_key", ""),
            expiry_date=tok_meta.get("expiry_date", ""),
            licence_type=tok_meta.get("subscription_type", "Perpetual"),
            machine_id=tok_meta.get("machine_id", ""),
            email=tok_meta.get("email", ""),
        )))
        log.info("[RemoteServer] MSG_LICENCE sent to %s", tok_meta.get("name", token[:8]))
    except Exception as exc:
        log.warning("[RemoteServer] Failed to send MSG_LICENCE to %s: %s", token[:8], exc)


def revoke_token(token: str) -> None:
    """Remove a token from the allowed list and disconnect the client."""
    name = _allowed_tokens.get(token, {}).get("name", token[:8])
    _allowed_tokens.pop(token, None)
    _pending.pop(token, None)        # also clear any re-registration attempts
    _revoked_tokens.add(token)       # remembered across server restarts
    _save_tokens()
    _save_pending()
    _save_revoked()
    log.info("[RemoteServer] Revoked %s — token added to revoke list", name)
    # Close the live connection if the client is currently connected.
    # Using .get() here (not .pop()) — let the handler's finally block clean up
    # _connected so the handler can finish processing any in-flight message first.
    entry = _connected.get(token)
    if entry:
        log.info("[RemoteServer] Sending MSG_REVOKE to %s (online)", name)
        # Clear the IP's failure count so the client can reconnect immediately
        # after the WS closes and receive the "revoked" notice without being
        # blocked by the rate limiter.
        client_ip = entry.get("info", {}).get("ip", "")
        if client_ip:
            _auth_failures.pop(client_ip, None)
        asyncio.create_task(_close_ws(entry["ws"], send_revoke=True))


async def _close_ws(ws, send_revoke: bool = False) -> None:
    if send_revoke:
        try:
            await ws.send(json.dumps(make(MSG_REVOKE)))
        except Exception:
            pass
    try:
        await ws.close()
    except Exception:
        pass


def _remember_build(token: str, version: str, commit_sha: str, commit_note: str) -> None:
    """Persist the build a client last reported onto its token record, so the
    admin console can still show it once that client goes offline.

    Without this the offline half of get_all_clients() had nothing to read:
    every disconnected client showed "unknown" version and no commit, because
    version/commit_sha only ever lived in the in-memory _connected entry that
    the disconnect throws away. In-memory only -- the callers that matter
    (HELLO, and the heartbeat's uptime accumulator) already _save_tokens()
    right after, so this doesn't add a disk write per heartbeat.
    """
    meta = _allowed_tokens.get(token)
    if meta is None:
        return
    if version and version != "?":
        meta["version"] = version
    # Only overwrite a known SHA with another known SHA -- a transient blank
    # (e.g. git briefly unreadable) shouldn't erase the last good answer.
    if commit_sha:
        meta["commit_sha"] = commit_sha
        meta["commit_note"] = ""
    elif commit_note:
        meta["commit_note"] = commit_note


def get_all_clients() -> list[dict]:
    """Return a combined list of connected + known-offline clients."""
    seen = set()
    result = []
    for token, entry in _connected.items():
        seen.add(token)
        info = dict(entry.get("info", {}))
        info["token"]     = token
        info["online"]    = True
        info["last_seen"] = entry.get("last_seen", 0)
        info["diag"]      = entry.get("diagnostics", {})
        tok_meta = _allowed_tokens.get(token, {})
        info["email"]             = tok_meta.get("email", "")
        info["nickname"]          = tok_meta.get("nickname", "")
        info["subscription_type"] = tok_meta.get("subscription_type", "")
        info["expiry_date"]       = tok_meta.get("expiry_date", "")
        info["total_uptime_s"]    = tok_meta.get("total_uptime_s", 0)
        result.append(info)
    for token, meta in _allowed_tokens.items():
        if token not in seen:
            result.append({
                "token":             token,
                "name":              meta.get("name", "?"),
                "email":             meta.get("email", ""),
                "nickname":          meta.get("nickname", ""),
                "online":            False,
                # Last build this client reported while it was connected
                # (_remember_build), not "unknown" -- an offline machine still
                # has a known version until it says otherwise.
                "version":           meta.get("version", "unknown"),
                "commit_sha":        meta.get("commit_sha", ""),
                "commit_note":       meta.get("commit_note", ""),
                "platform":          meta.get("platform", "unknown"),
                "hostname":          meta.get("hostname", "unknown"),
                "last_seen":         meta.get("last_seen", 0),
                "subscription_type": meta.get("subscription_type", ""),
                "expiry_date":       meta.get("expiry_date", ""),
                "total_uptime_s":    meta.get("total_uptime_s", 0),
                "diag":              {},
            })
    return result


def get_pending_registrations() -> list[dict]:
    return [{"token": t, **d} for t, d in _pending.items()]


# ── Rate limiter ─────────────────────────────────────────────────────────────

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    failures = _auth_failures.get(ip, [])
    failures = [t for t in failures if now - t < _FAILURE_WINDOW]
    _auth_failures[ip] = failures
    return len(failures) >= _MAX_FAILURES


def _record_failure(ip: str) -> None:
    now = time.time()
    lst = _auth_failures.get(ip, [])
    lst.append(now)
    _auth_failures[ip] = lst


def _read_version() -> str:
    # version_history.py's RELEASES[0] is the single source of truth — see
    # the matching comment in remote/client.py::_app_version() for why this
    # must not fall back to parsing CHANGELOG.md (free-text notes, drifts
    # independently — confirmed 3 releases stale).
    try:
        from forex_trader import __version__
        return __version__
    except Exception:
        pass
    ver_file = Path(__file__).parent.parent / "VERSION"
    if ver_file.exists():
        return ver_file.read_text(encoding="utf-8").strip()
    return "unknown"


def _read_changelog() -> list[str]:
    if _CHANGELOG_FILE.exists():
        return _CHANGELOG_FILE.read_text(encoding="utf-8").splitlines()[:40]
    return []


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def _handler(websocket) -> None:
    import websockets
    ip = websocket.remote_address[0] if websocket.remote_address else "?"

    # Auth timeout — drop connection if no HELLO within 5 s
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return

    try:
        msg = json.loads(raw)
    except Exception:
        return

    token       = msg.get("token", "")
    hostname    = msg.get("hostname", "?")
    platform    = msg.get("platform", "?")
    version     = msg.get("version", "?")
    commit_sha  = msg.get("commit_sha", "")
    commit_note = msg.get("commit_note", "")

    # ── Remote admin connection ───────────────────────────────────────────────
    if msg.get("type") == MSG_ADMIN_HELLO:
        uuid     = msg.get("machine_uuid", "")
        password = msg.get("password", "")
        if not uuid:
            try:
                await websocket.send(json.dumps(make(
                    MSG_REJECT, reason="no_uuid",
                    detail="Machine UUID could not be read on this device (macOS only).",
                )))
            except Exception:
                pass
            return
        if not is_admin_machine_uuid(uuid):
            log.warning("[RemoteServer] Admin hello from unrecognised UUID %s (%s)", uuid, ip)
            try:
                await websocket.send(json.dumps(make(
                    MSG_REJECT, reason="uuid_not_authorised",
                    detail=(
                        f"This machine has not been granted remote admin access.\n"
                        f"Add this UUID in the main admin panel → Remote Clients → "
                        f"Remote Admin Access:\n{uuid}"
                    ),
                    machine_uuid=uuid,
                )))
            except Exception:
                pass
            return
        from forex_trader.remote.auth import verify_password
        if not verify_password(password):
            log.warning("[RemoteServer] Admin hello from %s — wrong password", uuid[:8])
            try:
                await websocket.send(json.dumps(make(MSG_REJECT, reason="wrong_password",
                                                     detail="Incorrect admin password.")))
            except Exception:
                pass
            return
        await websocket.send(json.dumps(make(MSG_ADMIN_WELCOME)))
        await _admin_handler(websocket, uuid)
        return

    if msg.get("type") == MSG_REGISTER:
        # New client without a registered token — queue for admin approval.
        # Always overwrite so that a repeat attempt can add/update the email field.
        if token and token in _allowed_tokens:
            # Already approved (e.g. this MSG_REGISTER was in flight when an
            # admin approved it moments ago) — don't re-queue it as pending.
            log.info("[RemoteServer] Registration from %s (%s) ignored — "
                      "token already approved", hostname, ip)
        elif token:
            _pending[token] = {
                "hostname": hostname,
                "platform": platform,
                "version":  version,
                "email":    msg.get("email", ""),
                "nickname": msg.get("nickname", ""),
                "ts":       time.time(),
                "ip":       ip,
            }
            _save_pending()
            log.info("[RemoteServer] Registration request from %s (%s)", hostname, ip)
            # Notify any connected remote admin clients.
            asyncio.create_task(_push_pending_to_all_admins())
            asyncio.create_task(_notify_new_registration(
                hostname=hostname, email=msg.get("email", ""),
                nickname=msg.get("nickname", ""), ip=ip, token=token,
            ))
        await _close_ws(websocket)
        return

    if msg.get("type") != MSG_HELLO:
        return

    # Check revoked tokens BEFORE rate-limiting: a revoked client retrying its
    # old token should receive the "revoked" notice even if it was previously
    # rate-limited.  These reconnects are expected, not attack traffic.
    if token in _revoked_tokens:
        log.info("[RemoteServer] Revoked token reconnected from %s (%s) — sending revoke notice", hostname, ip)
        try:
            await websocket.send(json.dumps(make(MSG_REJECT, reason="revoked")))
        except Exception:
            pass
        return

    if _is_rate_limited(ip):
        log.warning("[RemoteServer] Rate-limited connection from %s", ip)
        await _close_ws(websocket)
        return

    if token not in _allowed_tokens:
        # Don't record this as a rate-limit failure — unknown tokens are
        # expected from new clients attempting registration, not attacks.
        # Only log at debug to avoid noise; real brute-forcing would use
        # random/malformed tokens and would be caught by the revoked-token
        # check above or by the connection-level rate limiter.
        log.info("[RemoteServer] Unknown token from %s (%s) — awaiting registration",
                 hostname, ip)
        try:
            await websocket.send(json.dumps(make(MSG_REJECT, reason="invalid_token")))
        except Exception:
            return
        # Keep the connection open so the client can send MSG_REGISTER on the
        # same connection — closing here would cause the client's send to fail.
        # Windows fingerprinting (get_fingerprint() -> wmic / PowerShell
        # Get-CimInstance fallback on 24H2+) shells out per-field with up to a
        # 6s timeout each, so the client's MSG_REGISTER can legitimately take
        # several seconds to arrive after MSG_REJECT — a short wait here drops
        # the registration silently (it shows up on neither admin console,
        # since it never reaches _pending at all).
        try:
            raw2 = await asyncio.wait_for(websocket.recv(), timeout=20.0)
            msg2  = json.loads(raw2)
            if msg2.get("type") == MSG_REGISTER:
                reg_token = msg2.get("token", "")
                if reg_token and reg_token in _allowed_tokens:
                    # This MSG_REGISTER was already in flight (built before the
                    # client knew it had been approved) — an admin approved the
                    # token while the client was still mid-fingerprint. Writing
                    # it back into _pending now would un-approve it from the
                    # admin UI's perspective even though _allowed_tokens already
                    # has a valid licence for it.
                    log.info("[RemoteServer] Registration from %s (%s) ignored — "
                              "token already approved", hostname, ip)
                elif reg_token:
                    _pending[reg_token] = {
                        "hostname":   msg2.get("hostname", hostname),
                        "platform":   msg2.get("platform", platform),
                        "version":    msg2.get("version", version),
                        "email":      msg2.get("email", ""),
                        "nickname":   msg2.get("nickname", ""),
                        "machine_id": msg2.get("machine_id", ""),
                        "ts":         time.time(),
                        "ip":         ip,
                    }
                    _save_pending()
                    # Successful registration clears any prior failure record
                    # so the client isn't penalised for its earlier attempts.
                    _auth_failures.pop(ip, None)
                    log.info("[RemoteServer] Registration request from %s (%s)",
                             msg2.get("hostname", hostname), ip)
                    asyncio.create_task(_push_pending_to_all_admins())
                    asyncio.create_task(_notify_new_registration(
                        hostname=msg2.get("hostname", hostname),
                        email=msg2.get("email", ""),
                        nickname=msg2.get("nickname", ""), ip=ip,
                        token=reg_token,
                    ))
        except asyncio.TimeoutError:
            log.info("[RemoteServer] No MSG_REGISTER from %s (%s) within 20s "
                      "after invalid_token — client did not follow up", hostname, ip)
        except Exception as exc:
            log.debug("[RemoteServer] Registration follow-up failed for %s (%s): %s",
                       hostname, ip, exc)
        return

    # Authenticated — welcome + push current version so client can check for updates
    current_version = _read_version()
    tok_meta = _allowed_tokens[token]
    machine_uuid = msg.get("machine_uuid", "")
    await websocket.send(json.dumps(make(
        MSG_WELCOME,
        subscription_type=tok_meta.get("subscription_type", "Perpetual"),
        expiry_date=tok_meta.get("expiry_date", "perpetual"),
        email=tok_meta.get("email", ""),
        is_remote_admin=is_admin_machine_uuid(machine_uuid),
    )))
    # If a licence key was generated for this client (via approve_registration),
    # deliver it now so the client can store it and activate.  Idempotent: the
    # client only restarts if the key differs from what it already holds.
    if tok_meta.get("licence_key"):
        await websocket.send(json.dumps(make(
            MSG_LICENCE,
            licence_key=tok_meta["licence_key"],
            expiry_date=tok_meta.get("expiry_date", ""),
            licence_type=tok_meta.get("subscription_type", "Perpetual"),
            machine_id=tok_meta.get("machine_id", ""),
            email=tok_meta.get("email", ""),
        )))
    await websocket.send(json.dumps(make(
        MSG_VERSION_INFO,
        latest=current_version,
        changelog=_read_changelog(),
    )))
    log.info("[RemoteServer] %s (%s) connected  version=%s  latest=%s",
             hostname, ip, version, current_version)

    client_info = {
        "name":         _allowed_tokens[token].get("name", hostname),
        "hostname":     hostname,
        "platform":     platform,
        "version":      version,
        "commit_sha":   commit_sha,
        "commit_note":  commit_note,
        "ip":           ip,
        "online":       True,
        "machine_uuid": machine_uuid,
    }
    _connected[token] = {
        "ws":          websocket,
        "info":        client_info,
        "last_seen":   time.time(),
        "diagnostics": {},
    }
    _allowed_tokens[token]["last_seen"] = time.time()
    _remember_build(token, version, commit_sha, commit_note)
    _save_tokens()
    # Remote admin consoles only see client online/offline state via this push —
    # unlike the main server's own admin UI, which reads _connected live on every
    # render. Without this, a remote console's client list goes stale the moment
    # a client connects/disconnects after the admin session was opened.
    await _push_clients_to_all_admins()

    try:
        async for raw_msg in websocket:
            if isinstance(raw_msg, bytes):
                continue  # no binary client→server messages in this protocol

            try:
                m = json.loads(raw_msg)
            except Exception:
                continue

            t = m.get("type")
            # Guard against revocation mid-session: token may have been removed
            # from _allowed_tokens while we were waiting for the next message.
            conn_entry = _connected.get(token)
            if conn_entry:
                conn_entry["last_seen"] = time.time()
            if token in _allowed_tokens:
                _allowed_tokens[token]["last_seen"] = time.time()

            if t == MSG_PONG:
                pass

            elif t == MSG_STATUS:
                if conn_entry:
                    conn_entry["info"].update({
                        "version":          m.get("version", version),
                        "commit_sha":       m.get("commit_sha", commit_sha),
                        "commit_note":      m.get("commit_note", commit_note),
                        "uptime_s":         m.get("uptime_s", 0),
                        "trades_open":      m.get("trades_open", 0),
                        "bridge_connected": m.get("bridge_connected", False),
                    })
                    # A client that updates itself mid-session reports the new
                    # build on its next heartbeat, not on a fresh HELLO.
                    _remember_build(
                        token,
                        conn_entry["info"]["version"],
                        conn_entry["info"]["commit_sha"],
                        conn_entry["info"]["commit_note"],
                    )
                    # Accumulate total time-online across sessions. Status
                    # heartbeats arrive every ~60s while connected; we add the
                    # gap between consecutive heartbeats (capped so a missed
                    # heartbeat / reconnect after sleep doesn't inflate the
                    # total) to a persisted per-token counter.
                    now = time.time()
                    prev_status_ts = conn_entry.get("_last_status_ts")
                    if prev_status_ts:
                        delta = min(now - prev_status_ts, 90)
                        if token in _allowed_tokens and delta > 0:
                            _allowed_tokens[token]["total_uptime_s"] = (
                                _allowed_tokens[token].get("total_uptime_s", 0) + delta
                            )
                            _save_tokens()
                    conn_entry["_last_status_ts"] = now

            elif t == MSG_DIAGNOSTICS:
                if conn_entry:
                    conn_entry["diagnostics"] = m.get("data", {})

            elif t == MSG_UPDATE_STATUS:
                status = m.get("status", "")
                log.info("[RemoteServer] Update status from %s: %s", hostname, status)

    except Exception:
        pass
    finally:
        _connected.pop(token, None)
        log.info("[RemoteServer] %s disconnected", hostname)
        await _push_clients_to_all_admins()


# ── Ping loop ─────────────────────────────────────────────────────────────────

async def _ping_loop() -> None:
    while True:
        await asyncio.sleep(30)
        dead = []
        for token, entry in list(_connected.items()):
            ws = entry["ws"]
            try:
                await ws.send(json.dumps(make(MSG_PING)))
            except Exception:
                dead.append(token)
        for t in dead:
            _connected.pop(t, None)


# ── LAN beacon — lets same-network clients skip NAT hairpinning ───────────────

_LAN_BEACON_PORT = 8444


def _get_local_ip() -> str:
    """Return this machine's LAN IP (the interface used to reach the internet)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


def _send_udp_broadcast(payload: bytes) -> None:
    """Blocking UDP broadcast — runs in a thread executor."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(payload, ("255.255.255.255", _LAN_BEACON_PORT))
    except OSError:
        pass


async def _lan_beacon_loop() -> None:
    """
    Broadcast local IP every 5 s so LAN clients can find the server without
    going via the WAN IP (which fails on most home routers due to NAT hairpinning).
    5-second interval + 6-second client probe window = guaranteed discovery on first attempt.
    """
    loop = asyncio.get_event_loop()
    _logged_once = False
    while True:
        local_ip = _get_local_ip()
        if local_ip:
            payload = json.dumps({
                "type": "forex_admin_beacon",
                "ip":   local_ip,
                "port": SERVER_PORT,
            }).encode()
            await loop.run_in_executor(None, _send_udp_broadcast, payload)
            if not _logged_once:
                log.info("[RemoteServer] LAN beacon started — local IP %s port %s",
                         local_ip, _LAN_BEACON_PORT)
                _logged_once = True
            else:
                log.debug("[RemoteServer] LAN beacon (local IP: %s)", local_ip)
        await asyncio.sleep(5)


# ── Push update to connected clients ────────────────────────────────────────
# Both functions just tell the client to self-update via git
# (core_app_update.apply_update(), the same code the client's own
# Settings > Update button runs) rather than streaming a zip built from
# this machine's local files. Keeping exactly one update implementation —
# on the client — means the admin-triggered path and the client's own
# self-service path can never drift out of sync with each other: the old
# zip push wrote files straight to disk with no git awareness at all,
# which left every client's working tree "dirty" and broke its own next
# git-based update (confirmed live — see the "Fix self-update apply_update()
# failing when the working tree has drifted" commit).

async def push_update(progress_cb=None) -> dict:
    """Trigger a git self-update on every connected client.
    progress_cb(msg: str) is called with status updates.

    Returns {"sent": N, "version": "x.y"}
    """
    if not _connected:
        return {"sent": 0, "version": _read_version()}

    def _update_progress(msg):
        if progress_cb:
            progress_cb(msg)

    version = _read_version()
    sent = 0
    for token, entry in list(_connected.items()):
        ws   = entry["ws"]
        name = entry["info"].get("name", token[:8])
        try:
            await ws.send(json.dumps(make(MSG_GIT_UPDATE)))
            sent += 1
            log.info("[RemoteServer] Sent MSG_GIT_UPDATE to %s", name)
            _update_progress(f"Triggered git update on {name}")
        except Exception as exc:
            log.warning("[RemoteServer] Failed to send MSG_GIT_UPDATE to %s: %s", name, exc)
            _update_progress(f"Failed to trigger update on {name}: {exc}")

    return {"sent": sent, "version": version}


async def push_update_to_client(token: str, progress_cb=None) -> dict:
    """Trigger a git self-update on a single connected client, identified by
    its token — same as push_update() but targeted, so updating one machine
    doesn't restart every other connected client too.

    Returns {"sent": 0 or 1, "version": "x.y"} or {"error": ...}.
    """
    entry = _connected.get(token)
    if not entry:
        return {"sent": 0, "version": _read_version(), "error": "Client not connected"}

    def _update_progress(msg):
        if progress_cb:
            progress_cb(msg)

    ws   = entry["ws"]
    name = entry["info"].get("name", token[:8])
    try:
        await ws.send(json.dumps(make(MSG_GIT_UPDATE)))
        log.info("[RemoteServer] Sent MSG_GIT_UPDATE to %s", name)
        _update_progress(f"Triggered git update on {name}")
        return {"sent": 1, "version": _read_version()}
    except Exception as exc:
        log.warning("[RemoteServer] Failed to send MSG_GIT_UPDATE to %s: %s", name, exc)
        _update_progress(f"Failed to trigger update on {name}: {exc}")
        return {"sent": 0, "version": _read_version(), "error": str(exc)}


async def request_diagnostics(token: str) -> bool:
    """Ask a specific client to send its diagnostics.  Returns False if not connected."""
    entry = _connected.get(token)
    if not entry:
        return False
    try:
        await entry["ws"].send(json.dumps(make(MSG_GET_DIAG)))
        return True
    except Exception:
        return False


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start() -> None:
    """Start the WebSocket server in a background asyncio task.

    Confirms external IP == 217.155.25.160 before binding so the server
    never accidentally starts on a remote user's machine even if they somehow
    obtained a copy of the admin password hash.
    """
    global _server_task
    _load_tokens()
    _load_admin_machines()
    # Re-sign step must happen after _load_tokens(), not at forex_admin.py's
    # import time -- register_kg_callbacks() runs during that earlier import,
    # before this module's _allowed_tokens dict has anything loaded into it,
    # so a resign attempted there always finds zero tokens to act on.
    resign_result = resign_all_licences()
    if resign_result.get("resigned"):
        log.info("[RemoteServer] Licence re-sign on startup: %s", resign_result)

    async def _run():
        global _server_obj
        import websockets
        from forex_trader.remote.ip_check import is_admin_machine, ADMIN_EXTERNAL_IP

        # IP gate — refuse to start on any machine that isn't the admin machine
        log.info("[RemoteServer] Verifying external IP before starting…")
        if not await is_admin_machine():
            log.warning(
                "[RemoteServer] External IP is not %s — server will NOT start. "
                "This machine is not the admin machine.", ADMIN_EXTERNAL_IP
            )
            return

        ssl_ctx = server_ssl_context()
        log.info("[RemoteServer] Starting on port %d (TLS) — external IP confirmed", SERVER_PORT)
        try:
            _server_obj = await websockets.serve(
                _handler,
                "0.0.0.0",
                SERVER_PORT,
                ssl=ssl_ctx,
                max_size=64 * 1024 * 1024,
                ping_interval=None,
            )
            asyncio.create_task(_ping_loop())
            asyncio.create_task(_lan_beacon_loop())
            log.info("[RemoteServer] Listening on 0.0.0.0:%d", SERVER_PORT)
            await _server_obj.wait_closed()
        except Exception as exc:
            log.error("[RemoteServer] Failed to start: %s", exc)

    loop = asyncio.get_event_loop()
    _server_task = loop.create_task(_run())


def stop() -> None:
    global _server_obj, _server_task
    if _server_obj:
        _server_obj.close()
        _server_obj = None
    if _server_task:
        _server_task.cancel()
        _server_task = None
    log.info("[RemoteServer] Stopped")
