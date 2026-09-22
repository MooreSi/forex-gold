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


# How long to wait for a handover before refusing. A restart is two instances
# overlapping on purpose -- the Windows path spawns the replacement and only
# then exits -- so the replacement must be allowed to outwait its own parent.
_SINGLE_INSTANCE_WAIT = 15.0


def _claim_single_instance() -> bool:
    """Take the one-app-per-data-directory lock. False means do not start.

    Two checkouts of this app resolve to the same USER_DATA_DIR: the same
    config.yaml, the same forex_trader_<env>.db, the same MT5 bridge port.
    Sharing them is the point -- it is what lets the owner switch between the
    two apps and keep one history. Running both at once is not.

    This must be called before `_free_port()`, which KILLS whatever is
    listening: without the lock, launching the second app terminated the first
    mid-trade and said nothing about it.

    **Fails open.** If the lock cannot be taken at all -- a read-only or
    missing data directory -- the app still starts, because refusing to boot
    is a worse failure than the overlap, and it is what every build before the
    lock did anyway. Logged as a warning, loudly, because a guard that has
    quietly stopped guarding is the thing this codebase keeps being bitten by.
    """
    from backend.src.utils import single_instance
    try:
        single_instance.acquire(timeout=_SINGLE_INSTANCE_WAIT)
        return True
    except single_instance.AlreadyRunning as exc:
        log.error(
            "%s Close the running app first (FOREX Stop.command / Stop "
            "FOREX.bat), or set FOREX_TRADER_DATA_DIR to give this checkout "
            "its own data directory.", exc,
        )
        return False
    except Exception as exc:
        log.warning(
            "Could not take the single-instance lock (%s) - starting anyway. "
            "Nothing is stopping a second app from opening the same database.",
            exc,
        )
        return True


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

        from urllib.parse import urlparse
        # 9010, not 9000: the fallback has to match the one the launch itself
        # uses (bridge_process.wine_bridge_launch), or the "is it already
        # running" probe below asks about a port nothing was ever started on.
        bridge_port = urlparse(bridge_url).port or 9010

        # A bridge that is already serving must be left alone. On macOS the
        # launch below is a fresh Wine session, and starting a second one
        # against a live terminal is how you get two MT5 windows — or, once
        # the watchdog reaches for its own restart, a wineserver teardown
        # that kills a terminal the app was happily trading through. A plain
        # app restart is the common case and must stay a 10-second event.
        from backend.src.utils.os_utils import is_port_listening
        if is_port_listening(bridge_port):
            log.info("MT5 bridge already listening on port %s — leaving it alone",
                     bridge_port)
            return None

        if sys.platform == "win32":
            cmd = [sys.executable, str(bridge_script)]
            # Without this, mt5_bridge.py falls back to its sibling-file default
            # instead of USER_DATA_DIR/bridge_credentials.json (the file the app
            # actually writes credentials to), so every cold boot connects with
            # no credentials at all — silently "self-healed" only because the
            # bridge watchdog's own restart path (engine.py's _start_bridge_process)
            # sets this env var correctly, masking the bug as a normal startup delay.
            from backend.src.config import USER_DATA_DIR
            bridge_env = {
                **os.environ,
                "BRIDGE_CREDS_PATH": str(USER_DATA_DIR / "bridge_credentials.json"),
                "MT5_BRIDGE_PORT": str(bridge_port),
            }
        else:
            # MT5 runs under Wine here, so the bridge is a Windows process and
            # the launch needs a bottle, a Z:-mapped script path and Z:-mapped
            # credentials. This used to ask for a WINE_PYTHON environment
            # variable instead — one that nothing in this repo has ever set,
            # so the branch was unreachable and the Mac never auto-started its
            # bridge at all (2026-09-21, after a power cut: 4m30s of dead app
            # before the watchdog ran this same launch itself and it worked).
            # Shared with that watchdog path so the two cannot drift again.
            from backend.src.services.broker.bridge_process import wine_bridge_launch
            cmd, bridge_env = wine_bridge_launch(str(bridge_script))

        log.info("Starting MT5 bridge: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            env=bridge_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
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


def _open_browser_when_up(port: int, timeout: float = 20.0) -> None:
    """Open the dashboard once the server is actually listening.

    `ui.run(show=True)` used to do this. Opening the browser before uvicorn has
    bound produces a connection-refused page that the operator then has to
    reload by hand, so this waits for the port in a background thread and only
    then opens it. Failing to open a browser must never stop the app.
    """
    import threading
    import webbrowser

    def _wait_and_open() -> None:
        from backend.src.utils.os_utils import is_port_listening
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_port_listening(port):
                try:
                    webbrowser.open(f"http://localhost:{port}")
                except Exception as exc:
                    log.warning("Could not open a browser (non-fatal): %s", exc)
                return
            time.sleep(0.25)
        log.warning("Server did not start listening on %s within %.0fs — "
                    "not opening a browser.", port, timeout)

    threading.Thread(target=_wait_and_open, daemon=True).start()


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

    # Before the config is read and long before _free_port() kills anything:
    # see _claim_single_instance.
    if not _claim_single_instance():
        return

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
        _db_path = str(_acct.db_path_for_env(
            cfg_module.DATA_DIR, _env, get_mt5_credentials()))
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

    # Off in the open-source build (backend/src/config/edition.py). The guard
    # and its activation screen are untouched and still tested -- this build
    # simply does not ask for a key, because the issuer that grants one is a
    # single machine the owner runs and a clone could never reach it.
    #
    # The two ordering rules above still hold whichever way the switch is set:
    # the database is open before this point, and nothing that can trade has
    # started yet. See tests/licence/test_activation_screen_has_a_database.py.
    from backend.src.config import edition as _edition

    if _edition.licence_required():
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
            # _db_path, NOT cfg["db_path"] -- the latter is the environment
            # default, so on a two-account install every automatic backup was
            # of a file the app no longer writes to (bugs/031). A backup of
            # the wrong database is worse than none: it looks like protection.
            made = _db_backup.maybe_daily_backup(_db_path, _DATA_DIR / "backups")
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

        # Serve the compiled React dashboard and the JSON API from one
        # uvicorn process. There is no second process and no Node runtime at
        # run time — the bundle in frontend/dist is built by a developer and
        # committed, which is what keeps this a Python-only install. See
        # docs/system/domains/frontend/010-the-react-decision.md.
        import uvicorn
        from backend.src.api.server import build_app
        from backend.src.utils.os_utils import register_ui_stopper

        # The login gate is off in the open-source build for the same reason
        # (edition.py). The middleware, the session endpoints and the password
        # store all remain: turning AUTHENTICATION_REQUIRED back on restores
        # the gate this app has always had, with nothing to rebuild.
        app = build_app(
            session_secret=_dashboard_storage_secret(),
            with_lifecycle=True,
            install_auth_gate=_edition.authentication_required(),
        )

        # Last thing before binding — see _claim_port. Everything above this
        # line (database open, app build) takes seconds, and the port was only
        # freed before all of it.
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

        if show_browser:
            _open_browser_when_up(port)

        # The ws_ping_interval / reconnect_timeout tuning that used to sit here
        # existed for NiceGUI's socket.io transport, which a backgrounded tab
        # kept losing and rebuilding (bugs/030 cause 3). There is no persistent
        # socket now: the dashboard polls on one shared interval that pauses
        # while the tab is hidden (frontend/src/hooks/usePoll.ts), so a
        # throttled background tab costs nothing to keep alive.
        server = uvicorn.Server(uvicorn.Config(
            app,
            host=_resolve_bind_host(cfg),
            port=port,
            log_level="info",
            access_log=False,
        ))
        # A Server instance rather than uvicorn.run(), because /restartapp and
        # the updater need a handle to stop it with. `should_exit` is uvicorn's
        # own graceful stop: it finishes in-flight requests and runs the
        # lifespan shutdown, which is what closes the bridge and the Telegram
        # reader. Killing the process instead would skip all of that.
        register_ui_stopper(lambda: setattr(server, "should_exit", True))
        server.run()
    finally:
        if bridge_proc:
            bridge_proc.terminate()
            log.info("MT5 bridge terminated")
        # The kernel would drop the lock at exit anyway, and does drop it
        # across the POSIX restart's os.execv (the fd is close-on-exec). This
        # is for the paths that return rather than exit -- headless, and the
        # port-claim refusal below -- so a re-entered main() is not waiting on
        # a lock this process never let go of.
        from backend.src.utils import single_instance
        single_instance.release()


if __name__ == "__main__":
    main()
