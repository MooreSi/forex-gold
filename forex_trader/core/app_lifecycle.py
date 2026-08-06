"""
App lifecycle — engine/bridge/Telegram/sync startup and shutdown.

Extracted out of forex_trader/ui/app.py so this logic can be called from two
different entry points: the normal NiceGUI-hosted app (ui/app.py's
@app.on_startup/@app.on_shutdown wrap the functions below) and the headless
entry point (run_headless.py), which never imports NiceGUI at all. Keeping
exactly one implementation here means a fix or a new sub-engine wired in here
automatically applies to both modes — nothing to remember to port twice.

Nothing in this module references nicegui.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import forex_trader.config as cfg_module
from forex_trader.core import database as db_module
from forex_trader.core.engine import SimulationEngine
from forex_trader.core.telegram_reader import TelegramReader

import forex_trader.test_signal.test_signal_service as _test_engine_module
import forex_trader.breakout_signal.breakout_signal_service as _breakout_engine_module
import forex_trader.reversal_engine.reversal_engine_service as _re_engine_module
import forex_trader.remote.client as _remote_client
import forex_trader.remote.server as _remote_server
from forex_trader.remote.auth import password_is_set

log = logging.getLogger(__name__)

# ── Admin panel — loaded from KeyGen (not shipped with FOREX) ─────────────────
# The admin UI lives in the KeyGen directory alongside FOREX on the admin's
# machine. Remote users don't have that directory, so the button never appears
# unless the server has granted them remote admin status (IOKit UUID match).
# Lives here (not ui/app.py) so both the NiceGUI and headless entry points
# see the same ADMIN_AVAILABLE value without duplicating the KeyGen lookup —
# ui/app.py imports admin_open_fn for its button's on_click handler.

def _find_admin_open_fn():
    """Look for KeyGen/forex_admin.py next to the FOREX directory.
    Adds KeyGen to sys.path if found and returns open_admin_dialog, else None."""
    forex_root = Path(__file__).parent.parent.parent  # forex_trader/core/app_lifecycle.py → FOREX/
    candidates = [
        forex_root.parent / "KeyGen",               # sibling: ~/Documents/KeyGen
        Path.home() / "Documents" / "KeyGen",        # explicit home fallback
    ]
    for kg_path in candidates:
        if (kg_path / "forex_admin.py").exists():
            if str(kg_path) not in sys.path:
                sys.path.insert(0, str(kg_path))
            try:
                import forex_admin as _fa
                log.info("[Admin] Loaded admin module from %s", kg_path)
                return _fa.open_admin_dialog
            except Exception as exc:
                log.warning("[Admin] Found forex_admin.py at %s but failed to import: %s",
                            kg_path, exc)
    log.debug("[Admin] KeyGen/forex_admin.py not found — admin button hidden")
    return None


def _find_remote_admin_open_fn():
    """Return open_dialog from KeyGen/admin_panel.py if this machine is a granted remote admin.

    The flag file is written by remote/client.py when the server confirms
    is_remote_admin=True in MSG_WELCOME. It persists across restarts so the
    button appears immediately on subsequent page loads without waiting for the
    server connection to re-establish.

    admin_panel.py lives in KeyGen/ (never shipped with the FOREX app) alongside
    forex_admin.py and admin_client.py."""
    from forex_trader.config import USER_DATA_DIR
    flag = Path(USER_DATA_DIR) / "remote" / "is_remote_admin"
    if not flag.exists():
        return None
    forex_root = Path(__file__).parent.parent.parent
    candidates = [
        forex_root.parent / "KeyGen",
        Path.home() / "Documents" / "KeyGen",
    ]
    for kg_path in candidates:
        if (kg_path / "admin_panel.py").exists():
            if str(kg_path) not in sys.path:
                sys.path.insert(0, str(kg_path))
            try:
                import admin_panel as _ap
                log.info("[Admin] Remote admin panel loaded from %s", kg_path)
                return _ap.open_dialog
            except Exception as exc:
                log.warning("[Admin] admin_panel.py found at %s but failed to import: %s",
                            kg_path, exc)
    log.warning("[Admin] Remote admin flag set but admin_panel.py not found in KeyGen")
    return None


admin_open_fn = _find_admin_open_fn()
if admin_open_fn is None:
    admin_open_fn = _find_remote_admin_open_fn()
ADMIN_AVAILABLE = admin_open_fn is not None

# ── App-wide singletons ──────────────────────────────────────────────────────

_engine:    Optional[SimulationEngine] = None
_tg_reader: Optional[TelegramReader]   = None


def get_engine() -> SimulationEngine:
    assert _engine is not None, "Engine not initialised"
    return _engine


def get_tg_reader() -> TelegramReader:
    assert _tg_reader is not None, "TelegramReader not initialised"
    return _tg_reader


async def _signal_engine_watchdog_loop() -> None:
    """App-level recovery loop: if any signal engine is enabled but not running,
    restart it. Runs every 5 minutes independently of each engine's own watchdog."""
    while True:
        await asyncio.sleep(300)
        try:
            from forex_trader.test_signal import test_signal_repo as _tdb
            if _tdb.get_config("sg_engine_enabled", "1") != "0":
                te = _test_engine_module.get_instance()
                if te and not te.is_running:
                    log.warning("[AppWatchdog] Bounce engine not running — auto-restarting")
                    te.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Bounce health check error: %s", _e)
        try:
            from forex_trader.breakout_signal import breakout_signal_repo as _bodb_wd
            if _bodb_wd.get_config("bo_engine_enabled", "1") != "0":
                bo = _breakout_engine_module.get_instance()
                if bo and not bo.is_running:
                    log.warning("[AppWatchdog] Breakout engine not running — auto-restarting")
                    bo.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Breakout health check error: %s", _e)
        try:
            from forex_trader.reversal_engine import reversal_engine_repo as _re_repo_wd
            if _re_repo_wd.get_config("re_user_stopped", "0") != "1":
                re_eng = _re_engine_module.get_instance()
                if re_eng and not re_eng.is_running:
                    log.warning("[AppWatchdog] Reversal Engine not running — auto-restarting")
                    re_eng.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Reversal Engine health check error: %s", _e)


async def startup() -> None:
    global _engine, _tg_reader
    config = cfg_module.load()
    db_module.init(config["db_path"])
    # Captures this thread's running loop so DB calls dispatched to the
    # dedicated worker thread (to_db_thread) can still schedule a sync
    # coroutine back onto it — see set_main_event_loop's docstring.
    import asyncio as _asyncio
    db_module.set_main_event_loop(_asyncio.get_running_loop())

    # Ensure bridge_credentials.json matches the active env so the bridge
    # connects to the right MT5 account on (re)start.
    env = config.get("account_env", "demo")
    db_module.sync_bridge_credentials_file(env)

    _engine    = SimulationEngine(config)
    _tg_reader = TelegramReader(config)
    _engine.set_telegram_reader(_tg_reader)

    # Initialise test signal DB immediately (before any async ops that might
    # fail) so the TEST tab can render even if the MT5 bridge is offline.
    _test_engine_module.init(_engine._bridge)

    # Initialise breakout signal DB (completely isolated from bounce engine).
    # breakout_signal_service.init() initializes its own repo DB internally.
    from forex_trader.config import DATA_DIR as _DATA_DIR
    _breakout_engine_module.init(_engine._bridge)

    # Initialise Reversal Engine DB (completely isolated from other engines).
    # 2026-07-23 rebrand (was "GD Copy Engine" / gd_copy_signal.db) -- an
    # install that already has the old file on disk gets it renamed in place
    # so its accumulated signal history and ML training data survive; a
    # fresh install just creates reversal_engine.db directly.
    _old_re_db = _DATA_DIR / "gd_copy_signal.db"
    _new_re_db = _DATA_DIR / "reversal_engine.db"
    if _old_re_db.exists() and not _new_re_db.exists():
        _old_re_db.rename(_new_re_db)
    from forex_trader.reversal_engine import reversal_engine_repo as _re_repo
    _re_repo.init(str(_new_re_db))
    # Reference-channel learning corpus. Lives in the RE db (one shared file)
    # rather than the per-env core db, and imports whatever the core db of
    # THIS environment already collected -- so demo and live train on the
    # same history. See reversal_engine/pro_corpus.py.
    try:
        from forex_trader.reversal_engine import pro_corpus as _pro_corpus
        _pro_corpus.init()
    except Exception as _e:
        log.error("[startup] Pro corpus init failed: %s", _e)
    from forex_trader.reversal_engine import ml_engine as _re_ml
    _re_ml.init(str(_DATA_DIR))
    _re_engine_module.init(_engine._bridge)

    # Protect each startup step independently so a failure in one does not
    # prevent the signal engine from starting.
    try:
        from forex_trader.core import loop_monitor as _loop_mon
        _loop_mon.start()
    except Exception as _e:
        log.error("[startup] Loop monitor failed to start: %s", _e)

    try:
        await _tg_reader.startup()
    except Exception as _e:
        log.error("[startup] TelegramReader startup failed: %s", _e)

    try:
        await _engine.startup()
    except Exception as _e:
        log.error("[startup] SimulationEngine startup failed: %s", _e)

    te = _test_engine_module.get_instance()
    te.set_main_engine(_engine)

    # Respect the persistent on/off preference saved by the Stop Engine button.
    # Default is enabled (first run or preference not set).
    from forex_trader.test_signal import test_signal_repo as _tdb
    if _tdb.get_config("sg_engine_enabled", "1") != "0":
        te.start()
        log.info("[startup] Signal engine auto-started")
    else:
        log.info("[startup] Signal engine auto-start suppressed (user disabled)")

    # Auto-start breakout engine — respect the persistent on/off preference.
    from forex_trader.breakout_signal import breakout_signal_repo as _bodb2
    bo_eng = _breakout_engine_module.get_instance()
    if bo_eng:
        bo_eng.set_main_engine(_engine)
        if _bodb2.get_config("bo_engine_enabled", "1") != "0":
            bo_eng.start()
            log.info("[startup] Breakout engine auto-started")
        else:
            log.info("[startup] Breakout engine auto-start suppressed (user disabled)")

    # Auto-start Reversal Engine — always on unless the user explicitly stopped it.
    # Uses re_user_stopped="1" (not re_engine_enabled) so normal app restarts
    # don't reset the preference; only a deliberate UI stop persists a "0".
    from forex_trader.reversal_engine import reversal_engine_repo as _re_repo2
    re_eng = _re_engine_module.get_instance()
    if re_eng:
        re_eng.set_main_engine(_engine)
        if _re_repo2.get_config("re_user_stopped", "0") != "1":
            re_eng.start()
            log.info("[startup] Reversal Engine auto-started")
        else:
            log.info("[startup] Reversal Engine skipped (user manually stopped)")

    # Local/Remote sync — resume whichever role was last configured so a
    # headless VPS reboot (nobody present to click the settings toggle) still
    # comes back up accepting connections, and the Mac reconnects to a
    # previously-configured VPS without re-entering the token.
    try:
        if db_module.get_app_config("sync_server_enabled") == "1":
            from forex_trader.sync import server as _sync_srv_mod
            token = db_module.get_sync_token()
            if token:
                import socket as _socket
                try:
                    host = _socket.gethostbyname(_socket.gethostname())
                except Exception:
                    host = "0.0.0.0"
                port = int(db_module.get_app_config("sync_server_port") or 8765)
                srv = _sync_srv_mod.init(
                    main_engine=_engine, breakout_engine=bo_eng,
                    bounce_engine=te, re_engine=re_eng,
                )
                await srv.start(host, port, token)
                log.info("[startup] Sync server auto-started on port %d", port)
            else:
                log.warning("[startup] sync_server_enabled but no token generated — "
                            "not starting sync server")
    except Exception as _e:
        log.error("[startup] Sync server auto-start failed: %s", _e)

    try:
        from forex_trader.sync.client import SyncClient
        _sc_host, _sc_port, _sc_token = SyncClient.load_config()
        if _sc_host and _sc_token:
            from forex_trader.sync import client as _sync_cli_mod
            _sync_cli_mod.get_instance().start(_sc_host, _sc_port, _sc_token)
            log.info("[startup] Sync client auto-connecting to %s:%d", _sc_host, _sc_port)
    except Exception as _e:
        log.error("[startup] Sync client auto-start failed: %s", _e)

    # App-level watchdog — recovers unexpected crashes every 5 min.
    asyncio.create_task(_signal_engine_watchdog_loop())

    # Remote admin: server only starts on admin machine (KeyGen present + password set).
    # Client only runs on non-admin machines — the admin Mac doesn't connect to itself.
    if ADMIN_AVAILABLE and password_is_set():
        _remote_server.start()
    elif config.get("remote_admin_client_enabled", False):
        _remote_client.start()
    else:
        log.info("[startup] Remote-admin client disabled (remote_admin_client_enabled=false) "
                 "— not connecting to the fleet admin server")

    log.info("FOREX Trader started on port %s (env=%s db=%s)",
             config.get("port", 8888), env, config["db_path"])


async def shutdown() -> None:
    te = _test_engine_module.get_instance()
    if te:
        te.stop()
    bo = _breakout_engine_module.get_instance()
    if bo:
        bo.stop()
    re_eng = _re_engine_module.get_instance()
    if re_eng:
        re_eng.stop(persist=False)
    _remote_client.stop()
    _remote_server.stop()
    try:
        from forex_trader.sync import server as _sync_srv_mod, client as _sync_cli_mod
        _srv = _sync_srv_mod.get_instance()
        if _srv:
            await _srv.stop()
        _sync_cli_mod.get_instance().stop()
    except Exception:
        pass
    if _engine:
        await _engine.shutdown()
    if _tg_reader:
        await _tg_reader.shutdown()
