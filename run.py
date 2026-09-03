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
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Ensure the project root is on the Python path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"

# Logging is deliberately NOT configured here -- see setup_logging() below,
# which main() calls. Importing a launcher is not consent to take over the root
# logger and start appending to a file another process is writing. The
# 2026-08-25 merge briefly had both: upstream's deferred setup_logging() landed
# while this module-scope block survived conflict resolution, so `import run`
# still hijacked the root logger and tests/test_log_isolation.py caught it.

log = logging.getLogger("forex_trader")

_LOGGING_READY = False


def setup_logging() -> None:
    """Attach the app's console + rotating-file logging to the root logger.

    Called from main(), NOT at import (2026-08-07). This used to run as a
    module-level side effect, which meant that merely importing `run` pointed
    the root logger at the LIVE app's forex_trader.log -- and
    tests/test_claim_port.py imports it, so every pytest session wrote into the
    running app's log.

    That was not just noise. A test run on 2026-08-07 put five WARNINGs into
    the production log reading "EA offline 601s with a healthy MT5 bridge --
    restarting the terminal (attempt 1/3)" and "terminal restart failed:
    wineserver would not die", none of which ever happened: they were fake
    durations from a fixture and an injected exception. Anyone reading that log
    to diagnose a real outage -- which is the whole reason it exists -- would
    have been chasing an event that never occurred.

    It also had two processes sharing one TimedRotatingFileHandler, so both
    would try to perform the midnight rename.

    Idempotent, so a re-import or a second call cannot double up handlers and
    write every line twice.
    """
    global _LOGGING_READY
    if _LOGGING_READY:
        return

    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

    # Write logs to a daily-rotating file in the user data directory.
    # Rotates at midnight; keeps 30 daily files before the oldest is removed.
    # Backup filenames:  forex_trader.log.YYYY-MM-DD
    #
    # Must match backend.src.config.USER_DATA_DIR exactly -- this checkout
    # (forex-refactor2) is a fork of the live app and must never default to its
    # "ForexTrader" folder (see the long comment in config.py for why).
    from backend.src.config import USER_DATA_DIR as _USER_DATA
    _log_dir = _USER_DATA / "data"
    _log_dir.mkdir(parents=True, exist_ok=True)
    fh = TimedRotatingFileHandler(
        _log_dir / "forex_trader.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    fh.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(fh)
    _LOGGING_READY = True


def _ensure_data_dirs():
    # Imported here, not at module scope: _setup_logging owns the only other
    # reference and binds it as a local, so a module-level name would be a
    # second source of truth for the same directory. Same import as line 175's.
    from backend.src.config import USER_DATA_DIR
    (USER_DATA_DIR / "data" / "sessions").mkdir(parents=True, exist_ok=True)


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


def _claim_port(port: int, timeout: float = 10.0) -> bool:
    """Free `port` and wait until it is genuinely free. True if it now is.

    Freeing the port once at the top of main() is not enough: the bind happens
    several seconds later, after the database opens and frontend.app is
    imported, and whatever claims the port inside that window wins. A licence
    activation is exactly when two instances exist -- the activation restart
    spawns a delayed relaunch, and a user who sees nothing happen launches the
    app themselves -- so the two come up seconds apart and one dies on bind.

    Confirmed live 2026-08-07 on a remote Mac: it activated, restarted, and
    then died with "[Errno 48] address already in use" roughly four seconds
    after freeing the port, with the Terminal window closing on a bare uvicorn
    error. From the user's side the app simply never came back, and neither
    the link nor the launcher would bring it up.

    Claiming the port immediately before ui.run() closes that window to
    approximately nothing.
    """
    try:
        from backend.src.utils.os_utils import is_port_listening
    except Exception:
        return True
    _free_port(port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not is_port_listening(port):
                return True
        except Exception:
            return True
        time.sleep(0.25)
        _free_port(port)
    return False


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


def _dashboard_storage_secret() -> str:
    """A stable per-install secret used to sign the login session cookie.

    Generated once and stored in the user data dir (never shipped). This only
    signs the dashboard session — it is not the licence or admin secret.
    """
    import secrets
    # Imported here, not at module scope: importing this launcher must stay
    # inert (see setup_logging above and tests/test_log_isolation.py).
    from backend.src.config import USER_DATA_DIR as _USER_DATA
    secret_file = _USER_DATA / "dashboard_storage_secret"
    try:
        if secret_file.exists():
            val = secret_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        val = secrets.token_urlsafe(48)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(val, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return val
    except OSError:
        # If the data dir isn't writable, fall back to an ephemeral secret so the
        # app still runs (sessions just won't survive a restart).
        return secrets.token_urlsafe(48)


def main():
    # First thing, before anything else here can log: everything below this
    # point expects the console and file handlers to already be attached.
    setup_logging()

    import argparse
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--no-browser", action="store_true",
                     help="Skip opening a new browser tab (used on restart)")
    _args, _ = _ap.parse_known_args()

    _ensure_data_dirs()
    _migrate_config_yaml()

    # ── Database first, so the licence screen behind it can function ─────────
    #
    # The activation screen does real work: it registers this machine and, on
    # the admin machine, brings up the admin server so a licence can be issued
    # locally. All of that reads this database. Opened after the licence check
    # it is never opened at all on that path -- enforce() shows the screen and
    # never returns -- and on 2026-09-02 that produced:
    #
    #   [RemoteServer] Registration Telegram notify failed:
    #                  no such table: telegram_config
    #
    # The owner sat on "awaiting administrator approval" waiting for a
    # notification that could not be sent, with nothing on screen saying why.
    #
    # Nothing about the licence DECISION moves: the guard reads a file in the
    # home directory, not this database. What moves is whether the screen it
    # shows can do its job. The engines still start only after the check.
    import backend.src.config as cfg_module
    cfg  = cfg_module.load()

    # ── Which database? One per MT5 account (owner, 2026-09-03) ──────────────
    #
    # The path used to be purely environment-based, so two demo accounts shared
    # one set of trades. It is now resolved per login as well.
    #
    # Nothing changes for an existing install: the first login seen for an
    # environment CLAIMS the existing forex_trader_<env>.db, so this file keeps
    # opening the same database it always did. Only adding a second account
    # creates anything, and that new database is seeded with the shared tables
    # -- credentials, risk settings, EA templates, learned rules -- so it opens
    # able to connect and trade rather than blank.
    #
    # Failure here falls back to cfg["db_path"], the environment default: the
    # app must start even if the registry cannot be read.
    from backend.src.db import account_registry as _acct
    from backend.src.db import database as _db_mod

    _db_path = cfg["db_path"]
    try:
        from backend.src.services.broker.credentials_repo import get_mt5_credentials
        _env   = cfg.get("account_env", "demo")
        _login = _acct.login_for_env(get_mt5_credentials(), _env)
        _db_path = str(_acct.resolve_db_path(
            cfg_module.DATA_DIR, _env, _login))
    except Exception as _exc:
        logging.getLogger(__name__).error(
            "[startup] could not resolve the per-account database (%s) — "
            "falling back to %s", _exc, _db_path)

    _db_mod.init(_db_path)

    # ── Licence check — must pass before any engine or UI starts ──────────────
    from backend.src.config.licence.guard import enforce as _licence_enforce
    from backend.src.config.licence.guard import register_activation_agent

    # bugs/021: the activation screen runs before TradingRuntime.startup(), so
    # nothing polls Telegram getUpdates there and the Approve button in a
    # registration alert does nothing. Register a restricted poller that serves
    # registration approvals ONLY. It is started by the guard on the admin
    # machine alone, and must be registered before enforce(), which shows the
    # screen and never returns.
    def _start_activation_approval_loop() -> None:
        import asyncio

        from backend.src.services.telegram.activation_bot import activation_bot_loop
        asyncio.create_task(activation_bot_loop(lambda: True))

    register_activation_agent(_start_activation_approval_loop)
    _licence_enforce()

    bridge_proc = _start_mt5_bridge()

    try:
        port = int(cfg.get("port", 8888))
        _free_port(port)

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
        # Gate every dashboard route behind a login (both modes). Registered here,
        # after the pages import and before ui.run, so the middleware is in place
        # when the server starts.
        from frontend import auth_gate as _auth_gate
        _auth_gate.install()

        # Last thing before binding — see _claim_port. Everything above this
        # line (database open, ui.app import) takes seconds, and the port was
        # only freed before all of it.
        if not _claim_port(port):
            from backend.src.utils.os_utils import pids_listening_on
            holders = pids_listening_on(port)
            log.error(
                "Port %s is still held by PID %s after repeated attempts to free "
                "it — not starting. Another FOREX Trader is most likely already "
                "running and serving on http://localhost:%s; open that instead. "
                "If it is wedged, stop it (FOREX Stop.command / Stop FOREX.bat) "
                "and start again.",
                port, ", ".join(map(str, holders)) or "unknown", port,
            )
            return

        ui.run(
            host=_resolve_bind_host(cfg),
            port=port,
            title="FOREX Trader",
            favicon="frontend/static/favicon.png",
            dark=True,
            reload=False,
            show=show_browser,
            # Signs the session cookie that backs app.storage.user — required for
            # the dashboard login gate (frontend/auth_gate.py). Per-install secret
            # stored in the user data dir; NOT a licence/auth secret.
            storage_secret=_dashboard_storage_secret(),
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
