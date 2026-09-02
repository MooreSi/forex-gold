"""Stateless helpers for the admin server: LAN discovery and the version it
advertises.

Moved verbatim out of `server.py` -- same functions, same bodies, same names,
imported back so every call site is unchanged.

These two sections were chosen because they touch NONE of the module's mutable
state. That mattered: `server.py` keeps ten module-level dicts and sets
(`_allowed_tokens`, `_pending`, `_connected`, `_admin_clients` and the rest)
which it rebinds with `global`, and splitting code that touches those would
fork the state -- each module getting its own copy, with writes landing in the
wrong one. See the note in `server.py` for why the rest of the file stays put.
"""
from __future__ import annotations

import json
import logging
import socket
import asyncio
from pathlib import Path

# The port the server listens on, advertised in the beacon payload. Imported
# rather than duplicated -- `remote/tls.py` is the one definition, and a second
# copy here would drift silently. (Left behind by the 2026-08-30 split; the
# beacon loop raised NameError on its first iteration.)
from backend.src.services.cluster.remote.tls import SERVER_PORT

log = logging.getLogger(__name__)


def _repo_root_for_files() -> Path:
    """Re-exported below from server.py's own definition; see there."""
    from backend.src.services.cluster.remote.server import _repo_root_for_files as _f
    return _f()


def _read_version() -> str:
    # version_history.py's RELEASES[0] is the single source of truth — see
    # the matching comment in remote/client.py::_app_version() for why this
    # must not fall back to parsing CHANGELOG.md (free-text notes, drifts
    # independently — confirmed 3 releases stale).
    try:
        from backend.src.utils.version_history import __version__
        return __version__
    except Exception:
        pass
    ver_file = _repo_root_for_files() / "VERSION"
    if ver_file.exists():
        return ver_file.read_text(encoding="utf-8").strip()
    return "unknown"


def _read_changelog() -> list[str]:
    """The changelog sent with MSG_VERSION_INFO on every successful connect.

    Resolved per call rather than at import: `server.py` computes its own
    `_CHANGELOG_FILE` at module level, and the 2026-08-30 split moved this
    function out without it, so every welcomed client hit a NameError here.
    Computing it here keeps the two files independent -- importing it back
    from `server.py` would be circular, since `server.py` imports this module.
    """
    changelog = _repo_root_for_files() / "CHANGELOG.md"
    if changelog.exists():
        return changelog.read_text(encoding="utf-8").splitlines()[:40]
    return []


# ── WebSocket handler ─────────────────────────────────────────────────────────


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


def registration_is_news(previous, details: dict) -> bool:
    """Has anything the admin acts on changed since this token last asked?

    Clients re-register on every reconnect, correctly. Announcing each one
    sent 139 Telegram messages in an hour for a single pending machine
    (docs/todo/bugs/021). `ts` is excluded from the comparison -- it moves on
    every request, so including it would announce every repeat and fix
    nothing.

    Pure, and here rather than in server.py for the reason this module exists:
    it touches none of that file's mutable state, and server.py is at its LOC
    ceiling. The caller passes `_pending.get(token)` -- keyed by TOKEN, because
    the admin approves a token and the same machine with a new one is news.
    """
    keys = ("hostname", "platform", "version", "email", "nickname", "ip")
    if previous is None:
        return True
    return any(previous.get(k) != details.get(k) for k in keys)
