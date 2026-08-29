"""
Remote client agent — runs in the background on every instance of the app.

Connects to the admin server at wss://217.155.25.160:8443, authenticates with
the per-machine UUID token, and handles:
  - Heartbeat / status reporting
  - Diagnostic report requests
  - Receiving and applying software updates

The agent reconnects automatically if the connection drops (e.g. admin machine
goes offline).  Remote users can still run the app normally when the server is
unavailable — the only thing they can't do is receive updates.
"""

import asyncio
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from backend.src.config import USER_DATA_DIR
from backend.src.config.licence import store as _licence_store
from backend.src import config as _app_config
from backend.src.services.cluster.remote.protocol import (
    MSG_HELLO, MSG_PONG, MSG_STATUS, MSG_DIAGNOSTICS, MSG_UPDATE_STATUS,
    MSG_REGISTER, MSG_WELCOME, MSG_REJECT, MSG_REVOKE, MSG_LICENCE,
    MSG_PING, MSG_GET_DIAG, MSG_GIT_UPDATE, MSG_VERSION_INFO, make,
)
from backend.src.services.cluster.remote.tls import client_ssl_context, SERVER_HOST, SERVER_PORT

log = logging.getLogger(__name__)

# Set when the server pushes a licence key and we've saved it to disk.
# guard.py polls this to navigate the browser gracefully before the restart.
licence_activated = threading.Event()

_LAN_BEACON_PORT = 8444


def _get_local_ip() -> str:
    """Return this machine's primary LAN IP (the interface used to reach the internet)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


def _blocking_probe_beacon(timeout_s: float) -> str:
    """
    Blocking UDP receive — listens for the admin server's LAN beacon.
    Returns the server's local IP string, or '' if nothing heard within timeout_s.

    SO_BROADCAST is required on Windows to receive broadcast packets; without it
    the OS silently drops them before the socket ever sees them.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)  # required on Windows
        try:
            sock.bind(("", _LAN_BEACON_PORT))
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, _ = sock.recvfrom(512)
                    msg = json.loads(data)
                    if msg.get("type") == "forex_admin_beacon":
                        ip = msg.get("ip", "")
                        if ip and ip != SERVER_HOST:
                            return ip
                except socket.timeout:
                    break
                except (json.JSONDecodeError, OSError):
                    continue
        finally:
            sock.close()
    except Exception:
        pass
    return ""


async def _scan_lan_for_server() -> str:
    """
    Parallel TCP probe of every host on the local /24 subnet looking for
    SERVER_PORT.  All 254 attempts run concurrently with a 0.5 s timeout each,
    so the total wall-clock time is ~0.5 s regardless of subnet size.

    Used as a fallback when the UDP beacon is not received (e.g. Windows
    Firewall blocks inbound UDP) and the WAN IP is unreachable from the LAN
    due to NAT hairpinning.
    """
    local_ip = _get_local_ip()
    if not local_ip:
        return ""
    prefix = ".".join(local_ip.split(".")[:3])  # e.g. "192.168.0"

    async def _probe(host: str) -> str:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, SERVER_PORT), timeout=0.5
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return host
        except Exception:
            return ""

    results = await asyncio.gather(*[_probe(f"{prefix}.{i}") for i in range(1, 255)])
    candidates = [ip for ip in results if ip and ip != local_ip]
    for ip in candidates:
        if await _speaks_our_protocol(ip):
            log.info("[RemoteClient] LAN scan found server at %s", ip)
            return ip
        log.info(
            "[RemoteClient] LAN scan: %s has port %d open but is not the admin "
            "server — ignoring", ip, SERVER_PORT,
        )
    return ""


async def _speaks_our_protocol(host: str) -> bool:
    """Return True only if `host` completes a WebSocket handshake on SERVER_PORT.

    An open TCP port is not proof of anything: 8443 is a common alternate-HTTPS
    port, so a router, NAS or hypervisor on the client's subnet answers the scan
    just as readily as the admin server does. Accepting the first open port
    outright meant the client locked onto that device and never fell back to the
    WAN address -- confirmed live 2026-08-07 on a remote Mac, which sat on the
    activation screen reporting "server rejected WebSocket connection: HTTP 404"
    while the real server was up and reachable the whole time. Rediscovery ran
    every retry, so it picked the same wrong host forever.

    The handshake is the cheapest thing only our server can pass; it runs on the
    handful of hosts the fast TCP scan shortlisted, not the whole subnet.
    """
    import websockets
    try:
        async with websockets.connect(
            f"wss://{host}:{SERVER_PORT}",
            ssl=client_ssl_context(),
            open_timeout=4,
            close_timeout=2,
        ):
            return True
    except Exception:
        return False


async def _discover_lan_server(timeout: float = 6.0) -> str:
    """
    Listen for the admin server's UDP LAN beacon.
    Returns the server's local IP if heard within *timeout* seconds, else ''.
    Falls back to a subnet TCP scan if the beacon is not received.
    """
    loop = asyncio.get_event_loop()
    ip = await loop.run_in_executor(None, _blocking_probe_beacon, timeout)
    if ip:
        log.info("[RemoteClient] LAN beacon received — connecting via local IP %s", ip)
        return ip

    # Beacon not heard (e.g. Windows Firewall blocked the UDP).
    # Fall back to a fast parallel TCP scan of the local subnet.
    log.debug("[RemoteClient] No beacon — scanning local subnet for server on port %s", SERVER_PORT)
    ip = await _scan_lan_for_server()
    return ip

_REMOTE_DIR    = Path(USER_DATA_DIR) / "remote"
_TOKEN_FILE    = _REMOTE_DIR / "client_token.txt"
_EMAIL_FILE    = _REMOTE_DIR / "client_email.txt"
_NICKNAME_FILE = _REMOTE_DIR / "client_nickname.txt"

_START_TIME  = time.time()
_client_task: Optional[asyncio.Task] = None
_status: dict = {
    "connected":           False,
    "last_error":          "",
    "version":             "unknown",
    "latest_version":      "",
    "update_available":    False,
    "changelog":           [],
    "connected_host":      "",
    "subscription_type":   "",
    "subscription_expiry": "",
    "email":               "",
    "nickname":            "",
    "is_remote_admin":     False,
    # Unix time the last MSG_REGISTER actually reached the admin server, 0 if
    # none has. Reset by request_registration() so each new request is judged
    # on its own delivery, not an earlier one's.
    "registration_sent_at": 0.0,
}


# ── Token management ─────────────────────────────────────────────────────────

def get_or_create_token() -> str:
    """Return this machine's persistent UUID token, creating it on first run."""
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    if _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(tok) == 64:  # 32 random bytes = 64 hex chars
            return tok
    tok = secrets.token_hex(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def get_status() -> dict:
    return dict(_status)


def get_stored_email() -> str:
    if _EMAIL_FILE.exists():
        return _EMAIL_FILE.read_text(encoding="utf-8").strip()
    return ""


def get_stored_nickname() -> str:
    if _NICKNAME_FILE.exists():
        return _NICKNAME_FILE.read_text(encoding="utf-8").strip()
    return ""


def request_registration(email: str, nickname: str = "") -> None:
    """Store email/nickname and immediately restart the connection loop so MSG_REGISTER is sent."""
    global _client_task
    _REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    _EMAIL_FILE.write_text(email.strip(), encoding="utf-8")
    if nickname.strip():
        _NICKNAME_FILE.write_text(nickname.strip(), encoding="utf-8")
    _status["registration_sent_at"] = 0.0
    _status["last_error"] = "contacting administrator server"
    if _client_task and not _client_task.done():
        _client_task.cancel()
    try:
        loop = asyncio.get_running_loop()
        _client_task = loop.create_task(_connect_loop())
    except RuntimeError:
        pass


def _app_version() -> str:
    # version_history.py's RELEASES[0] is the single source of truth —
    # forex_trader/__init__.py derives __version__ from it directly and
    # keeps the VERSION file in sync as a fallback for callers that can't
    # import the package itself. CHANGELOG.md is free-text release notes
    # only and is maintained separately, so it must never be used to derive
    # the version number — it drifts (confirmed: sat 3 releases behind
    # VERSION), which made this function silently report a stale version
    # both here and via the HELLO handshake the admin console reads.
    try:
        from backend.src.utils.version_history import __version__
        return __version__
    except Exception:
        pass
    from backend.src.utils.os_utils import repo_root
    ver_file = repo_root() / "VERSION"
    if ver_file.exists():
        return ver_file.read_text(encoding="utf-8").strip()
    return "unknown"


def _commit_report() -> tuple[str, str]:
    """(sha, reason) for the code this instance is running -- see
    core_app_update.get_commit_report(). The reason travels with the
    heartbeat so the admin console can distinguish a machine that has simply
    never self-updated (no .git yet) from one whose checkout is broken,
    instead of showing the same blank "no commit" for both."""
    try:
        from backend.src.services.positions.core_app_update import get_commit_report
        return get_commit_report()
    except Exception as e:
        log.debug("[RemoteClient] commit report unavailable: %s", e)
        return "", "unavailable"


def _build_hello() -> dict:
    import platform
    from backend.src.services.cluster.remote.ip_check import get_machine_uuid
    commit_sha, commit_note = _commit_report()
    return make(
        MSG_HELLO,
        token=get_or_create_token(),
        version=_app_version(),
        commit_sha=commit_sha,
        commit_note=commit_note,
        platform=sys.platform,
        hostname=platform.node(),
        machine_uuid=get_machine_uuid(),
        ts=time.time(),
    )


def _build_register() -> dict:
    import platform
    from backend.src.config.licence.fingerprint import get_fingerprint
    return make(
        MSG_REGISTER,
        token=get_or_create_token(),
        version=_app_version(),
        platform=sys.platform,
        hostname=platform.node(),
        email=get_stored_email(),
        nickname=get_stored_nickname(),
        machine_id=get_fingerprint(),
        ts=time.time(),
    )


def _get_resource_usage() -> dict:
    """CPU/memory snapshot for this node, sent with every status heartbeat
    to the admin panel's Remote Clients card. cpu_percent(interval=None) and
    virtual_memory() are both fast, non-blocking OS reads (no disk/network
    I/O), so no thread hop is needed."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "cpu_percent":  psutil.cpu_percent(interval=None),
            "mem_used_mb":  round((vm.total - vm.available) / (1024 * 1024), 1),
            "mem_total_mb": round(vm.total / (1024 * 1024), 1),
            "mem_percent":  vm.percent,
        }
    except Exception:
        return {}


def _build_status() -> dict:
    uptime = int(time.time() - _START_TIME)
    trades_open = 0
    bridge_ok   = False
    is_native   = False
    try:
        from backend.src.db import database as _db
        trades_open = _db.get_open_trade_count() or 0
    except Exception:
        pass
    try:
        # Native mode has no HTTP bridge to poll at all — checking port 9000
        # there always fails and misreports a healthy in-process connection
        # as "Bridge Offline". Read the live engine's own connection state
        # directly instead; only fall back to the HTTP health check when
        # actually running the HTTP bridge.
        from backend.src.app import get_engine
        engine = get_engine()
        is_native = bool(getattr(engine, "_using_native_bridge", False))
    except Exception:
        engine = None
    if is_native:
        try:
            mod = engine._bridge._mod
            bridge_ok = bool(mod is not None and mod._connected)
        except Exception:
            bridge_ok = False
    else:
        try:
            from backend.src.config import get as _cfg_get
            import urllib.request as _ureq
            if _cfg_get("mt5_bridge_enabled", True):
                _url = _cfg_get("mt5_bridge_url", "http://localhost:9000").rstrip("/")
                _ureq.urlopen(_url + "/health", timeout=0.5)
                bridge_ok = True
        except Exception:
            bridge_ok = False
    commit_sha, commit_note = _commit_report()
    return make(
        MSG_STATUS,
        version=_app_version(),
        commit_sha=commit_sha,
        commit_note=commit_note,
        uptime_s=uptime,
        trades_open=trades_open,
        bridge_connected=bridge_ok,
        bridge_native=is_native,
        **_get_resource_usage(),
    )


_DIAG_NOISY = (
    "GET /tick/", "GET /position", "GET /account", "GET /health",
    "POST /order", "GET /ping", "/tick/XAUUSD", "HTTP/1.",
)


def _log_level(line: str) -> str:
    """Extract levelname from a log line produced by _LOG_FORMAT.

    Format: "YYYY-MM-DD HH:MM:SS,mmm LEVELNAME logger — message"
    Timestamp is always 23 chars, then a space, then the level.
    Using split avoids the old [24:31] slice which only worked for WARNING
    (7 chars) and silently dropped INFO (4 chars) and ERROR (5 chars).
    """
    if len(line) <= 24:
        return ""
    return line[24:].split(" ", 1)[0]


def _build_diagnostics() -> dict:
    import platform
    from datetime import datetime, timedelta
    commit_sha, commit_note = _commit_report()
    data = {
        "platform":  sys.platform,
        "hostname":  platform.node(),
        "python":    sys.version,
        "version":   _app_version(),
        "commit_sha": commit_sha,
        "commit_note": commit_note,
        "uptime_s":  int(time.time() - _START_TIME),
        "log_lines": [],
        "log_raw":   "",
    }
    try:
        from backend.src.config import DATA_DIR
        log_file = Path(DATA_DIR) / "forex_trader.log"
        if log_file.exists():
            cutoff = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
            raw = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            # Full raw log (last 150 KB worth of lines) for the download button
            raw_tail = raw[-3000:]
            data["log_raw"] = "\n".join(raw_tail)
            # Filtered view: past 24 h, errors/warnings/significant info
            kept: list[str] = []
            for ln in reversed(raw[-8000:]):
                if len(ln) >= 16 and ln[:16] < cutoff:
                    break
                if not ln.strip():
                    continue
                level = _log_level(ln)
                if level in ("ERROR", "WARNING"):
                    kept.append(ln)
                elif level == "INFO" and not any(n in ln for n in _DIAG_NOISY):
                    kept.append(ln)
            data["log_lines"] = list(reversed(kept))[-500:]
    except Exception:
        pass
    return make(MSG_DIAGNOSTICS, data=data)


from backend.src.services.cluster.remote._update import (  # noqa: E402
    _RESTART_EXIT_CODE, _apply_git_update, _do_restart, _refresh_desktop_icon,
)


async def _connect_loop() -> None:
    import websockets
    ssl_ctx  = client_ssl_context()
    token    = get_or_create_token()
    backoff  = 10  # initial retry delay (seconds)

    while True:
        # 1. Listen for the UDP LAN beacon (6 s window, beacon fires every 5 s).
        # 2. If beacon not heard, scan the local /24 subnet via TCP (~0.5 s).
        # 3. If still nothing, fall back to the hardcoded WAN IP.
        lan_ip = await _discover_lan_server(timeout=6.0)
        host   = lan_ip if lan_ip else SERVER_HOST
        url    = f"wss://{host}:{SERVER_PORT}"

        try:
            async with websockets.connect(
                url,
                ssl=ssl_ctx,
                max_size=64 * 1024 * 1024,
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                # Send hello
                await ws.send(json.dumps(_build_hello()))

                # Wait for welcome or reject
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    msg = json.loads(raw)
                except Exception:
                    msg = {}

                if msg.get("type") == MSG_REJECT:
                    reason = msg.get("reason", "?")
                    if reason == "revoked":
                        # Admin explicitly revoked this client.  Delete both the
                        # licence key file (gates the first-install screen) and the
                        # WS token file, then hard-exit so the delayed restart
                        # subprocess brings the app back on the registration screen.
                        try:
                            _licence_store.clear()
                        except Exception:
                            pass
                        try:
                            _TOKEN_FILE.unlink(missing_ok=True)
                        except Exception:
                            pass
                        log.info("[RemoteClient] Token revoked by administrator (reject path) — restarting clean")
                        _do_restart()  # os._exit(0) — never returns
                    elif reason == "invalid_token":
                        # Server stays open 5 s after MSG_REJECT to receive this.
                        # Wrapped in try-except so a close-race never doubles backoff.
                        try:
                            await ws.send(json.dumps(_build_register()))
                            # Positive proof the request actually left this
                            # machine. This is the ONLY place a registration is
                            # ever sent, and it needs a live connection to the
                            # admin server to get here -- so the activation
                            # screen must not claim "request sent" off the back
                            # of the button click alone (confirmed live
                            # 2026-08-07: it told a user their request was
                            # awaiting approval while the admin server was down
                            # and nothing had been transmitted at all).
                            _status["registration_sent_at"] = time.time()
                            log.info("[RemoteClient] Registration request sent — awaiting admin approval")
                        except Exception:
                            log.info("[RemoteClient] Token rejected — awaiting admin approval")
                        backoff = 10  # reset so approval is detected within 10 s
                        _status["last_error"] = "awaiting admin approval"
                    else:
                        _status["last_error"] = f"rejected: {reason}"
                    _status["connected"] = False
                    await asyncio.sleep(backoff)
                    continue

                if msg.get("type") != MSG_WELCOME:
                    _status["connected"] = False
                    await asyncio.sleep(backoff)
                    continue

                _status["connected"]           = True
                _status["last_error"]          = ""
                _status["connected_host"]      = host
                _status["subscription_type"]   = msg.get("subscription_type", "")
                _status["subscription_expiry"] = msg.get("expiry_date", "")
                _status["email"]               = msg.get("email", "")
                is_admin = bool(msg.get("is_remote_admin", False))
                _status["is_remote_admin"]     = is_admin
                # Persist the flag so the admin button appears on next page load
                # even before the server connection is re-established.
                _flag_file = _REMOTE_DIR / "is_remote_admin"
                try:
                    if is_admin:
                        _flag_file.touch()
                    else:
                        _flag_file.unlink(missing_ok=True)
                except Exception:
                    pass

                # A successful MSG_WELCOME means the server already has this
                # token in its approved list. Persist that so app_lifecycle.py's
                # startup check (config.get("remote_admin_client_enabled"),
                # default False) auto-starts this loop on every future launch —
                # without this, the only thing that ever calls _connect_loop()
                # is the manual Settings > Request Registration button, which
                # calls it directly and never touches this config key at all.
                if not _app_config.get("remote_admin_client_enabled", False):
                    try:
                        _app_config.save_to_yaml({"remote_admin_client_enabled": True})
                    except Exception as _cfg_err:
                        log.warning(
                            "[RemoteClient] Could not persist remote_admin_client_enabled: %s",
                            _cfg_err,
                        )

                backoff = 10  # reset on success
                log.info("[RemoteClient] Connected to admin server (%s)%s",
                         host, " [remote admin]" if is_admin else "")

                # Periodic status sender
                last_status = 0.0

                async for raw_msg in ws:
                    if isinstance(raw_msg, bytes):
                        continue

                    try:
                        m = json.loads(raw_msg)
                    except Exception:
                        continue

                    t = m.get("type")

                    if t == MSG_VERSION_INFO:
                        latest = m.get("latest", "")
                        my_ver = _app_version()
                        _status["latest_version"]   = latest
                        _status["changelog"]        = m.get("changelog", [])
                        _status["update_available"] = bool(
                            latest and latest != my_ver
                        )
                        if _status["update_available"]:
                            log.info(
                                "[RemoteClient] Update available: %s → %s",
                                my_ver, latest,
                            )

                    elif t == MSG_PING:
                        await ws.send(json.dumps(make(MSG_PONG)))

                    elif t == MSG_GET_DIAG:
                        await ws.send(json.dumps(_build_diagnostics()))

                    elif t == MSG_GIT_UPDATE:
                        await ws.send(json.dumps(make(
                            MSG_UPDATE_STATUS, status="applying"
                        )))
                        asyncio.create_task(_apply_git_update())

                    elif t == MSG_LICENCE:
                        # Admin approved and the server generated the licence key.
                        # Store it on disk, then restart — guard.enforce() will find
                        # the key on the next boot and let the main app load normally.
                        lic_key  = m.get("licence_key", "")
                        expiry   = m.get("expiry_date", "")
                        ltype    = m.get("licence_type", "Perpetual")
                        email    = m.get("email", get_stored_email())
                        mid      = m.get("machine_id", "")
                        if lic_key and expiry:
                            # Verify before storing. guard.enforce() now sends a
                            # client with an unverifiable key back to the
                            # activation screen (instead of a dead-end error),
                            # and that screen reconnects here — so saving a key
                            # that does not verify would restart the app straight
                            # into the same screen, forever. An admin console
                            # still running a retired signing scheme is exactly
                            # the case that would trigger it. Reject it here
                            # instead and stay on the activation screen.
                            from backend.src.config.licence.fingerprint import get_fingerprint
                            from backend.src.config.licence.verify import verify_licence_key
                            if not verify_licence_key(mid or get_fingerprint(), expiry, lic_key):
                                log.error(
                                    "[RemoteClient] Rejected pushed licence — signature does not "
                                    "verify for this machine (admin console may be running an "
                                    "older signing scheme). Staying on the activation screen."
                                )
                                continue
                            existing = _licence_store.load()
                            if not existing or existing.get("licence_key") != lic_key:
                                from backend.src.config.licence.fingerprint import get_fingerprint
                                _licence_store.save({
                                    "machine_id":   mid or get_fingerprint(),
                                    "email":        email,
                                    "expiry_date":  expiry,
                                    "licence_type": ltype,
                                    "licence_key":  lic_key,
                                })
                                log.info("[RemoteClient] Licence received — signalling UI then restarting")
                                # Signal guard.py's timer to navigate the browser to a
                                # clean "Activating..." page before we kill the process.
                                # Give it 3 s; if it hasn't acted we force the exit.
                                licence_activated.set()
                                await asyncio.sleep(3.0)
                                _do_restart()
                                break

                    elif t == MSG_REVOKE:
                        # Admin revoked this client — delete the licence key file
                        # (gates the first-install screen) and the WS token file,
                        # then hard-exit so the restart subprocess brings the app
                        # back on the registration screen.
                        try:
                            _licence_store.clear()
                        except Exception:
                            pass
                        try:
                            _TOKEN_FILE.unlink(missing_ok=True)
                        except Exception:
                            pass
                        log.info("[RemoteClient] Token revoked by administrator — restarting clean")
                        _do_restart()
                        break

                    # Send status every 60 s without waiting for messages
                    now = time.time()
                    if now - last_status > 60:
                        try:
                            await ws.send(json.dumps(_build_status()))
                        except Exception:
                            break
                        last_status = now

            # Connection closed cleanly (server closed WS without an exception —
            # e.g. server shutdown or revocation).  Reset status so the UI stops
            # showing "connected" before the next reconnect attempt.
            _status["connected"]      = False
            _status["connected_host"] = ""

        except Exception as exc:
            was_connected = _status["connected"]
            _status["connected"] = False
            _status["connected_host"] = ""
            _status["last_error"] = str(exc)
            log_fn = log.info if was_connected else log.warning
            log_fn("[RemoteClient] Connection error: %s: %s (retry in %ds)",
                   type(exc).__name__, exc, backoff)
            await asyncio.sleep(backoff)
            if was_connected:
                # Dropped an active connection (network change, server restart, etc.)
                # — reconnect quickly rather than backing off.
                backoff = 10
            else:
                backoff = min(backoff * 2, 60)  # cap at 60 s for failed connect attempts


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start() -> None:
    """Start the client agent background task."""
    global _client_task
    _status["version"] = _app_version()
    loop = asyncio.get_event_loop()
    _client_task = loop.create_task(_connect_loop())
    log.info("[RemoteClient] Agent started (connecting to %s:%d)", SERVER_HOST, SERVER_PORT)


def stop() -> None:
    global _client_task
    if _client_task:
        _client_task.cancel()
        _client_task = None
    _status["connected"] = False
