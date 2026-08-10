"""
FOREX Trader — launcher.
Run: python run.py
Opens the dashboard in your default browser (see backend.src.config's
default port for this checkout).
"""

import logging
import os
import subprocess
import sys
import socket
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Ensure the project root is on the Python path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
)

# Write logs to a daily-rotating file in the user data directory.
# Rotates at midnight; keeps 30 daily files before the oldest is removed.
# Backup filenames:  forex_trader.log.YYYY-MM-DD
#
# Must match backend.src.config.USER_DATA_DIR exactly -- this checkout
# (forex-refactor2) is a fork of the live app and must never default to its
# "ForexTrader" folder (see the long comment in config.py for why).
from backend.src.config import USER_DATA_DIR as _USER_DATA
_log_dir   = _USER_DATA / "data"
_log_dir.mkdir(parents=True, exist_ok=True)
_fh = TimedRotatingFileHandler(
    _log_dir / "forex_trader.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8",
    utc=False,
)
_fh.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_fh)

log = logging.getLogger("forex_trader")


def _ensure_data_dirs():
    (_USER_DATA / "data" / "sessions").mkdir(parents=True, exist_ok=True)


def _free_port(port: int) -> None:
    """Kill any process already listening on the given port."""
    try:
        from backend.src.utils.os_utils import free_port as _fp, pids_listening_on
        pids = pids_listening_on(port)
        _fp(port)
        if pids:
            log.info("Freed port %s (killed pid %s)", port, ", ".join(map(str, pids)))
    except Exception as e:
        log.debug("Could not free port %s: %s", port, e)


def _start_mt5_bridge() -> subprocess.Popen | None:
    """
    If mt5_bridge_enabled is true in config and the bridge script exists,
    start mt5_bridge.py as a subprocess.  The bridge must run under Wine Python
    on macOS.  On Windows it can run directly.
    Returns the Popen object or None if not started.
    """
    try:
        import backend.src.config as cfg_module
        cfg = cfg_module.load()
        if not cfg.get("mt5_bridge_enabled", True):
            log.info("MT5 bridge disabled in config — skipping")
            return None

        if sys.platform == "win32" and cfg.get("mt5_native_bridge_enabled", True):
            log.info("Native in-process MT5 bridge enabled — skipping the separate "
                     "mt5_bridge.py subprocess (the main app imports MetaTrader5 directly).")
            return None

        bridge_url = cfg.get("mt5_bridge_url", "")
        if bridge_url and "localhost" not in bridge_url and "127.0.0.1" not in bridge_url:
            log.info("MT5 bridge configured at %s — assuming externally managed", bridge_url)
            return None

        bridge_script = ROOT / "mt5_bridge.py"
        if not bridge_script.exists():
            log.info("mt5_bridge.py not found — skipping bridge auto-start")
            return None

        if sys.platform == "win32":
            cmd = [sys.executable, str(bridge_script)]
        else:
            wine_python = os.environ.get("WINE_PYTHON", "")
            if not wine_python:
                log.info("WINE_PYTHON not set — skipping bridge auto-start")
                return None
            cmd = [wine_python, str(bridge_script)]

        # Without this, mt5_bridge.py falls back to its sibling-file default
        # instead of USER_DATA_DIR/bridge_credentials.json (the file the app
        # actually writes credentials to), so every cold boot connects with
        # no credentials at all — silently "self-healed" only because the
        # bridge watchdog's own restart path (engine.py's _start_bridge_process)
        # sets this env var correctly, masking the bug as a normal startup delay.
        from backend.src.config import USER_DATA_DIR
        from urllib.parse import urlparse
        bridge_port = urlparse(bridge_url).port or 9000
        bridge_env = {
            **os.environ,
            "BRIDGE_CREDS_PATH": str(USER_DATA_DIR / "bridge_credentials.json"),
            "MT5_BRIDGE_PORT": str(bridge_port),
        }

        log.info("Starting MT5 bridge: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, env=bridge_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log.info("MT5 bridge started (pid=%s)", proc.pid)
        return proc
    except Exception as e:
        log.warning("Could not start MT5 bridge: %s", e)
        return None


_CLAUDE_ALIASES = {
    "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-6",
    "claude-opus-4-5":   "claude-opus-4-8",
}


def _migrate_config_yaml() -> None:
    """
    Rewrite any stale Claude model IDs in config.yaml before any Python module
    reads the config.  This is the earliest possible interception point — it runs
    before config.py, settings.py or any UI code is imported, so old copies of
    those files on remote machines cannot cause a crash.
    """
    from backend.src.config import CONFIG_FILE as cfg_path

    if not cfg_path.exists():
        return
    try:
        import re
        text = cfg_path.read_text(encoding="utf-8")
        changed = False
        for old, new in _CLAUDE_ALIASES.items():
            # Match the key with optional surrounding whitespace/quotes, any line ending
            new_text = re.sub(
                rf'(?m)^(\s*claude_model\s*:\s*["\']?){re.escape(old)}(["\']?\s*)$',
                rf'\g<1>{new}\g<2>',
                text,
            )
            if new_text != text:
                text = new_text
                changed = True
                log.info("Migrated config.yaml claude_model: %s → %s", old, new)
        if changed:
            cfg_path.write_text(text, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not migrate config.yaml: %s", exc)


def _run_headless() -> None:
    """Run the trading engine, Telegram reader, MT5 bridge client, and sync
    server/client with no web UI at all — no NiceGUI, no uvicorn ASGI server,
    no Socket.IO service tasks, none of the per-page ui.timer() refreshers.
    Used when headless_mode_enabled is set (toggled via /headless on|off),
    typically on the VPS where an unattended browser tab was directly
    implicated in event-loop stalls and memory pressure.

    Calls the exact same core.app_lifecycle.startup()/shutdown() the NiceGUI
    path uses via @app.on_startup/@app.on_shutdown — one implementation, two
    entry points, so nothing needs porting separately between modes.
    """
    import asyncio
    import signal as _signal

    async def _main() -> None:
        from backend.src import app as app_lifecycle
        await app_lifecycle.startup()
        log.info("Headless FOREX Trader running (no web UI). Send /headless off "
                 "in Telegram to restore the dashboard on next restart.")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        # SIGTERM/SIGINT handlers aren't supported on Windows' default
        # ProactorEventLoop (raises NotImplementedError) — Ctrl+C there is
        # already delivered as a normal KeyboardInterrupt, which propagates
        # out of stop_event.wait() below and still runs the finally block.
        if sys.platform != "win32":
            for sig in (_signal.SIGINT, _signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
        try:
            await stop_event.wait()
        finally:
            await app_lifecycle.shutdown()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


# Hosts that are only reachable from the same machine. Binding the dashboard
# anywhere else exposes an unauthenticated, live-trading UI to the network.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(host: str) -> bool:
    """True only for addresses reachable solely from this machine."""
    return str(host).strip().lower() in _LOOPBACK_HOSTS


def _resolve_bind_host(cfg: dict) -> str:
    """The address the dashboard binds to — loopback unless deliberately widened.

    The UI has no login of its own and can place and close live orders, so a
    non-loopback bind is a network-exposed trading terminal. We still allow it
    (some future networked deployment behind real auth will need it) but never
    silently: a non-loopback host gets a loud warning every launch.
    """
    host = str(cfg.get("host", "127.0.0.1"))
    if not _is_loopback(host):
        log.warning(
            "Dashboard is binding to non-loopback host %r. This UI has no login "
            "and can place and close live orders — anyone who can reach this "
            "address controls the account. Only widen the bind once real "
            "authentication is in place.",
            host,
        )
    return host


def main():
    import argparse
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--no-browser", action="store_true",
                     help="Skip opening a new browser tab (used on restart)")
    _args, _ = _ap.parse_known_args()

    _ensure_data_dirs()
    _migrate_config_yaml()

    # ── Licence check — must pass before any engine or UI starts ──────────────
    from backend.src.config.licence.guard import enforce as _licence_enforce
    _licence_enforce()

    bridge_proc = _start_mt5_bridge()

    try:
        import backend.src.config as cfg_module
        cfg  = cfg_module.load()
        port = int(cfg.get("port", 8888))
        _free_port(port)

        from backend.src.db import database as _db_mod
        _db_mod.init(cfg["db_path"])

        # Daily snapshot of the live-money DB (at most one per day, keep 30) to a
        # local backups/ folder. Non-fatal: a backup problem must never stop the
        # app from starting.
        try:
            from backend.src.db import backup as _db_backup
            from backend.src.config import DATA_DIR as _DATA_DIR
            made = _db_backup.maybe_daily_backup(cfg["db_path"], _DATA_DIR / "backups")
            if made:
                log.info("Daily DB backup written: %s", made)
        except Exception as exc:
            log.warning("Daily DB backup failed (non-fatal): %s", exc)

        if _db_mod.get_app_config("headless_mode_enabled") == "1":
            log.info("Headless mode enabled — starting without the web UI.")
            _run_headless()
            return

        log.info("Starting FOREX Trader on http://localhost:%s", port)

        # Never auto-open a browser on a Remote-role (VPS/sync-server) instance,
        # regardless of what triggered this launch (initial start, /restartapp,
        # an applied update, or anything else) — an unattended browser tab left
        # running on a resource-constrained VPS was directly implicated in
        # event-loop stalls and memory pressure. Checked here, once, centrally,
        # rather than relying on every restart call site remembering to pass
        # --no-browser, so it can't be missed by a future code path. Local
        # (Mac) instances are completely unaffected — this only ever turns
        # the browser off, never on, so --no-browser still works as before.
        show_browser = not _args.no_browser
        try:
            if _db_mod.get_app_config("sync_server_enabled") == "1":
                show_browser = False
                log.info("This instance is configured as the Remote (VPS) node — "
                         "skipping automatic browser launch. Open "
                         "http://localhost:%s manually if you need the dashboard.", port)
        except Exception as exc:
            log.warning("Could not check Remote-node role for browser auto-launch: %s", exc)

        from nicegui import ui
        import frontend.app  # registers startup hooks and page routes  # noqa: F401

        ui.run(
            host=_resolve_bind_host(cfg),
            port=port,
            title="FOREX Trader",
            favicon="frontend/static/favicon.png",
            dark=True,
            reload=False,
            show=show_browser,
            # Keep the WebSocket alive when the browser tab is backgrounded or
            # the screen is locked.  Browsers throttle background tabs, so the
            # default 3 s reconnect window and 20 s uvicorn ping timeout cause
            # spurious "Connection lost" overlays on Windows clients.
            reconnect_timeout=30,          # wait 30 s before showing reconnect overlay
            ws_ping_interval=30,           # server→browser ping every 30 s
            ws_ping_timeout=60,            # allow 60 s for a pong before closing
        )
    finally:
        if bridge_proc:
            bridge_proc.terminate()
            log.info("MT5 bridge terminated")


if __name__ == "__main__":
    main()
