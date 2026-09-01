"""
App lifecycle — engine/bridge/Telegram/sync startup and shutdown.

Extracted out of frontend/app.py so this logic can be called from two
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

import backend.src.config as cfg_module
from backend.src.db import database as db_module
from backend.src.runtime import TradingRuntime
from backend.src.services.telegram.reader import TelegramReader

import backend.src.services.test_signal.test_signal_service as _test_engine_module
import backend.src.services.breakout_signal.breakout_signal_service as _breakout_engine_module
import backend.src.services.reversal_engine.reversal_engine_service as _re_engine_module
import backend.src.services.cluster.remote.client as _remote_client
import backend.src.services.cluster.remote.server as _remote_server
from backend.src.services.cluster.remote.auth import password_is_set

log = logging.getLogger(__name__)


def _make_tg_reader(config: dict):
    """Composition-root choice: the Telethon reader normally, the scripted
    FakeTelegramReader in debug mode (offline — see local-debug-mode 030).
    Services stay swap-unaware; only this entry point decides."""
    if config.get("debug_mode"):
        from backend.src.services.telegram.fake_reader import FakeTelegramReader
        scenario = _load_debug_scenario()
        return FakeTelegramReader(config, scenario=scenario)
    return TelegramReader(config)


def _load_debug_scenario() -> Optional[dict]:
    """The default debug scenario (tools/debug_scenarios/tp1-hit.json), or
    None for the seeded synthetic stream if the file is missing/bad."""
    import json
    path = Path(__file__).parent.parent.parent / "tools" / "debug_scenarios" / "tp1-hit.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("[debug] scenario file %s unavailable (%s) — synthetic stream", path, exc)
        return None


# ── Admin panel — loaded from KeyGen (not shipped with FOREX) ─────────────────
# The admin UI lives in the KeyGen directory alongside FOREX on the admin's
# machine. Remote users don't have that directory, so the button never appears
# unless the server has granted them remote admin status (IOKit UUID match).
# Lives here (not ui/app.py) so both the NiceGUI and headless entry points
# see the same ADMIN_AVAILABLE value without duplicating the KeyGen lookup —
# ui/app.py imports admin_open_fn for its button's on_click handler.

def _import_with_timeout(module_name: str, timeout: float = 20.0):
    """Import `module_name` on a worker thread, giving up after `timeout`.

    These KeyGen modules run real work at import time — forex_admin.py opens
    the licence registry SQLite file at module level — and that work can block
    forever rather than fail. Confirmed live 2026-08-07: iCloud Drive had
    evicted ~/Documents/KeyGen/licences.db (file flagged "dataless") and could
    no longer download it back, so sqlite3.connect() never returned. The app
    logged "Starting FOREX Trader on http://localhost:8888", hung inside this
    module's import, and never reached ui.run() — it never bound the port, so
    it looked like a silent failure to start, with nothing in the log and no
    traceback. Three processes ended up wedged on the same open().

    The admin console is optional; the trading app must start without it. A
    plain import cannot be interrupted, so run it on a daemon thread and walk
    away if it stalls — the thread stays stuck, but the process exits normally
    and the app carries on with the admin button hidden.
    """
    import importlib
    import threading

    outcome: dict = {}

    def _work():
        try:
            outcome["module"] = importlib.import_module(module_name)
        except BaseException as exc:      # noqa: BLE001 — reported, not swallowed
            outcome["error"] = exc

    thread = threading.Thread(target=_work, daemon=True, name=f"import-{module_name}")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        log.error(
            "[Admin] Importing %s blocked for over %.0fs — continuing without the "
            "admin console. This usually means a file it opens at import time is "
            "unreadable rather than missing (an iCloud-evicted 'dataless' file, a "
            "stalled network mount). Check: ls -lO ~/Documents/KeyGen",
            module_name, timeout,
        )
        return None
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("module")


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
                _fa = _import_with_timeout("forex_admin")
                if _fa is None:
                    return None
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
    from backend.src.config import USER_DATA_DIR
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
                _ap = _import_with_timeout("admin_panel")
                if _ap is None:
                    return None
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

_engine:    Optional[TradingRuntime] = None
_tg_reader: Optional[TelegramReader]   = None


def get_engine() -> TradingRuntime:
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
            from backend.src.services.test_signal import test_signal_repo as _tdb
            if _tdb.get_config("sg_engine_enabled", "1") != "0":
                te = _test_engine_module.get_instance()
                if te and not te.is_running:
                    log.warning("[AppWatchdog] Bounce engine not running — auto-restarting")
                    te.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Bounce health check error: %s", _e)
        try:
            from backend.src.services.breakout_signal import breakout_signal_repo as _bodb_wd
            if _bodb_wd.get_config("bo_engine_enabled", "1") != "0":
                bo = _breakout_engine_module.get_instance()
                if bo and not bo.is_running:
                    log.warning("[AppWatchdog] Breakout engine not running — auto-restarting")
                    bo.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Breakout health check error: %s", _e)
        try:
            from backend.src.services.reversal_engine import reversal_engine_repo as _re_repo_wd
            if _re_repo_wd.get_config("re_user_stopped", "0") != "1":
                re_eng = _re_engine_module.get_instance()
                if re_eng and not re_eng.is_running:
                    log.warning("[AppWatchdog] Reversal Engine not running — auto-restarting")
                    re_eng.start()
        except Exception as _e:
            log.debug("[AppWatchdog] Reversal Engine health check error: %s", _e)


def _remote_client_enabled(config) -> bool:
    """Whether to start the outbound remote-admin/update client.

    Default ON since 2026-08-26 (Q001 #5, amended). Simon uses the admin
    console for licence permissions and to see which clients are online, so a
    client that never connects is a broken feature.

    The old default was OFF because the channel applied pushed CODE with no
    signature check. That is no longer true: upstream 0815cc6 deleted the
    zip-streaming push, and an admin "update" now only asks the client to run
    its own git pull. The warning below describes what is ACTUALLY still
    unauthenticated, because a warning that names a risk which no longer exists
    trains people to ignore warnings.

    Kept as a single predicate so the "is it on?" decision is testable and
    cannot drift away from the warning.
    """
    if not config.get("remote_admin_client_enabled", True):
        return False
    log.warning(
        "remote-admin client starting. The link to the admin server runs TLS "
        "with certificate verification DISABLED and no certificate pinning "
        "(remote/tls.py), so someone on the network path can impersonate the "
        "server. What they can trigger is now limited to a git pull from this "
        "checkout's own remote, not arbitrary code. Set "
        "remote_admin_client_enabled=false to stay off the fleet; certificate "
        "pinning is the tracked fix."
    )
    return True


async def startup() -> None:
    global _engine, _tg_reader
    config = cfg_module.load()
    db_module.init(config["db_path"])
    # Captures this thread's running loop so DB calls dispatched to the
    # dedicated worker thread (to_db_thread) can still schedule a sync
    # coroutine back onto it — see set_main_event_loop's docstring.
    import asyncio as _asyncio
    _loop = _asyncio.get_running_loop()
    db_module.set_main_event_loop(_loop)

    # Hold a strong reference to every fire-and-forget task until it finishes.
    # asyncio.create_task is called 183 times here with the result discarded --
    # alerts, profit syncs, admin pushes -- and the event loop keeps only WEAK
    # references, so any of them can be collected mid-execution. Nothing raises
    # when that happens; the alert just never arrives. One factory covers every
    # call site, including ones written later.
    from backend.src.utils import background_tasks as _background_tasks
    _background_tasks.install(_loop)

    # Ensure bridge_credentials.json matches the active env so the bridge
    # connects to the right MT5 account on (re)start.
    env = config.get("account_env", "demo")
    db_module.sync_bridge_credentials_file(env)

    # Re-arm the auto-restart watchdog. The stop scripts disarm it so that Stop
    # genuinely stops, which means arming has to happen on the way back up --
    # otherwise the first clean stop would silently retire supervision for good.
    from backend.src.services.positions import core_autostart as _autostart
    _autostart.sync_from_setting(
        db_module.get_app_config("auto_restart_enabled") == "1"
    )

    _engine    = TradingRuntime(config)
    _tg_reader = _make_tg_reader(config)
    _engine.set_telegram_reader(_tg_reader)

    # Initialise test signal DB immediately (before any async ops that might
    # fail) so the TEST tab can render even if the MT5 bridge is offline.
    _test_engine_module.init(_engine._bridge)

    # Initialise breakout signal DB (completely isolated from bounce engine).
    # breakout_signal_service.init() initializes its own repo DB internally.
    from backend.src.config import DATA_DIR as _DATA_DIR
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
    from backend.src.services.reversal_engine import reversal_engine_repo as _re_repo
    _re_repo.init(str(_new_re_db))
    # Reference-channel learning corpus. Lives in the RE db (one shared file)
    # rather than the per-env core db, and imports whatever the core db of
    # THIS environment already collected -- so demo and live train on the
    # same history. See reversal_engine/pro_corpus.py.
    try:
        from backend.src.services.reversal_engine import pro_corpus_repo as _pro_corpus
        _pro_corpus.init()
    except Exception as _e:
        log.error("[startup] Pro corpus init failed: %s", _e)
    from backend.src.services.reversal_engine import ml_engine as _re_ml
    _re_ml.init(str(_DATA_DIR))
    _re_engine_module.init(_engine._bridge)

    # Protect each startup step independently so a failure in one does not
    # prevent the signal engine from starting.
    try:
        from backend.src.utils import loop_monitor as _loop_mon
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
        log.error("[startup] TradingRuntime startup failed: %s", _e)

    te = _test_engine_module.get_instance()
    te.set_main_engine(_engine)

    # Respect the persistent on/off preference saved by the Stop Engine button.
    # Default is enabled (first run or preference not set).
    from backend.src.services.test_signal import test_signal_repo as _tdb
    if _tdb.get_config("sg_engine_enabled", "1") != "0":
        te.start()
        log.info("[startup] Signal engine auto-started")
    else:
        log.info("[startup] Signal engine auto-start suppressed (user disabled)")

    # Auto-start breakout engine — respect the persistent on/off preference.
    from backend.src.services.breakout_signal import breakout_signal_repo as _bodb2
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
    from backend.src.services.reversal_engine import reversal_engine_repo as _re_repo2
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
            from backend.src.services.cluster.sync import server as _sync_srv_mod
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
        from backend.src.services.cluster.sync.client import SyncClient
        _sc_host, _sc_port, _sc_token = SyncClient.load_config()
        if _sc_host and _sc_token:
            from backend.src.services.cluster.sync import client as _sync_cli_mod
            _sync_cli_mod.get_instance().start(_sc_host, _sc_port, _sc_token)
            log.info("[startup] Sync client auto-connecting to %s:%d", _sc_host, _sc_port)
    except Exception as _e:
        log.error("[startup] Sync client auto-start failed: %s", _e)

    # App-level watchdog — recovers unexpected crashes every 5 min.
    asyncio.create_task(_signal_engine_watchdog_loop())

    # News calendar: start the background refresher so the signal engines read a
    # cached snapshot instead of doing up to ~10s of blocking urllib on the event
    # loop every cycle (backend review 2026-08-08, #5).
    from backend.src.utils import news_calendar as _news
    _news.ensure_started()

    # Remote admin: server only starts on admin machine (KeyGen present + password set).
    # Client only runs on non-admin machines — the admin Mac doesn't connect to itself.
    if ADMIN_AVAILABLE and password_is_set():
        _remote_server.start()
    elif _remote_client_enabled(config):
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
    from backend.src.utils import news_calendar as _news
    _news.stop()
    try:
        from backend.src.services.cluster.sync import server as _sync_srv_mod, client as _sync_cli_mod
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
