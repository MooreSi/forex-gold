"""
MT5 Bridge — runs under Wine Python on Mac.

This script must be executed via Wine Python inside the same Wine prefix
as the MetaTrader 5 terminal.  It does NOT run in the macOS Python environment.

Usage (after setup_wine_bridge.sh):
    ./start_bridge.sh

What it does:
    Connects to the MT5 terminal (running in Wine), reads live XAUUSD data and
    account information, then serves them over a local HTTP server on port 9000.
    The FastAPI service (running in macOS Python) calls these endpoints.

Endpoints:
    GET /health
    GET /tick/XAUUSD
    GET /candles/XAUUSD?timeframe=M5&count=200
    GET /account
    GET /positions
    GET /history?days=7
    POST /reconnect

Dependencies (inside Wine Python only):
    MetaTrader5   (pip install MetaTrader5)
    No other external packages — stdlib only.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────

PORT             = int(os.environ.get("MT5_BRIDGE_PORT", "9000"))
SYMBOL           = os.environ.get("MT5_SYMBOL", "XAUUSD")
MT5_LOGIN        = int(os.environ.get("MT5_LOGIN", "0"))
MT5_PASSWORD     = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER       = os.environ.get("MT5_SERVER", "")
MT5_TIMEOUT_MS   = int(os.environ.get("MT5_TIMEOUT_MS", "60000"))
RECONNECT_EVERY  = int(os.environ.get("MT5_RECONNECT_EVERY_S", "30"))

# Credentials file — set by the launcher as a Wine Z: path pointing to
# ~/Library/Application Support/ForexTrader/bridge_credentials.json.
# Falls back to sibling file for manual/legacy invocations.
CONFIG_PATH = os.environ.get("BRIDGE_CREDS_PATH") or \
              os.path.join(os.path.dirname(__file__), "bridge_credentials.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mt5_bridge")

# ── MetaTrader5 import (must be available in Wine Python) ─────────────────────

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    _MT5_AVAILABLE = False
    log.error("MetaTrader5 package not found. Run setup_wine_bridge.sh first.")

# ── Timeframe map ─────────────────────────────────────────────────────────────

_TF_MAP = {
    "M1":  1,    # mt5.TIMEFRAME_M1
    "M5":  5,    # mt5.TIMEFRAME_M5
    "M15": 15,
    "M30": 30,
    "H1":  16385,  # mt5.TIMEFRAME_H1
    "H4":  16388,  # mt5.TIMEFRAME_H4
    "D1":  16408,  # mt5.TIMEFRAME_D1
}

_TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}

# ── Connection state ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_connected = False
_last_error = ""
_connect_time = 0.0


def _load_credentials() -> tuple[int, str, str, str]:
    """Load from env vars first, then bridge_credentials.json.
    Returns (login, password, server, terminal_path)."""
    login         = MT5_LOGIN
    password      = MT5_PASSWORD
    server        = MT5_SERVER
    terminal_path = ""

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            login         = login    or int(cfg.get("login", 0))
            password      = password or cfg.get("password", "")
            server        = server   or cfg.get("server", "")
            terminal_path = cfg.get("terminal_path", "") or ""
        except Exception as e:
            log.warning("Could not read bridge_credentials.json: %s", e)
    return login, password, server, terminal_path


def _connect() -> bool:
    global _connected, _last_error, _connect_time
    if not _MT5_AVAILABLE:
        _last_error = "MetaTrader5 package not installed"
        return False

    login, password, server, terminal_path = _load_credentials()
    if not login or not password:
        _last_error = "No credentials configured. Set MT5_LOGIN/MT5_PASSWORD or bridge_credentials.json"
        log.error(_last_error)
        return False

    try:
        # Every other MT5 API call in this module runs under _mt5_call_lock —
        # connect/reconnect must too, or the background _reconnect_loop thread
        # can call mt5.shutdown()/initialize()/login() at the exact moment
        # another thread is mid-call inside a locked API call (e.g. _get_tick's
        # tick pump, which runs every 250ms). That race can leave the MT5 IPC
        # channel torn down under an in-flight call, which then blocks forever
        # with no timeout — the confirmed cause of the "severe freeze" pattern
        # seen specifically under the native (in-process) bridge, where a wedged
        # call holds NativeMT5Bridge's own asyncio.Lock forever, freezing every
        # subsequent bridge-dependent operation in the whole app.
        with _mt5_call_lock:
            # Always try to attach to the already-running MT5 terminal first (no path
            # argument).  Passing path= when MT5 is already running causes the Python
            # library to launch a *second* terminal window instead of reusing the
            # existing one — only supply path= as a fallback when no instance is found.
            if mt5.initialize(timeout=MT5_TIMEOUT_MS):
                log.info("mt5.initialize() attached to existing MT5 instance")
            else:
                mt5.shutdown()   # reset internal state before retrying
                if not terminal_path:
                    _last_error = f"mt5.initialize() failed: {mt5.last_error()}"
                    log.error(_last_error)
                    return False
                log.info("No running MT5 found; launching via path: %s", terminal_path)
                if not mt5.initialize(path=terminal_path, timeout=MT5_TIMEOUT_MS):
                    _last_error = f"mt5.initialize() failed: {mt5.last_error()}"
                    log.error(_last_error)
                    return False

            # Brief pause — gives the terminal time to settle the IPC connection
            # before we attempt login, especially after a shutdown/initialize cycle.
            time.sleep(0.5)

            auth = mt5.login(login=login, password=password, server=server)
            if not auth:
                err = mt5.last_error()
                _last_error = f"mt5.login() failed: {err}"
                log.error(_last_error)
                mt5.shutdown()
                return False

            info = mt5.account_info()

        if info is None:
            _last_error = "Connected but account_info() returned None"
            log.warning(_last_error)
        else:
            # Login masked: the diagnostics feature uploads ~3,000 raw log
            # lines to the admin server, and this line put the account number,
            # the broker server and the balance into every one of them (Q005 #1
            # in docs/simon-handover/005-fact-finding.md -- confirmed from real
            # captured logs). The last 3 digits are enough to tell two accounts
            # apart in a support conversation. Balance dropped for the same
            # reason; it is on the dashboard, where it belongs.
            _login = str(info.login or "")
            _masked = ("*" * max(0, len(_login) - 3)) + _login[-3:] if len(_login) > 3 else "***"
            log.info(
                "Connected to MT5. Login: %s, Server: %s, Currency: %s",
                _masked, info.server, info.currency,
            )

        with _lock:
            _connected = True
            _last_error = ""
            _connect_time = time.time()
        return True

    except Exception as e:
        _last_error = str(e)
        log.exception("MT5 connect exception")
        return False


def _ensure_connected() -> bool:
    with _lock:
        if _connected:
            return True
    return _connect()


def _mark_disconnected(reason: str) -> None:
    """Thread-safe: set _connected=False and record the reason."""
    global _connected, _last_error
    with _lock:
        _connected  = False
        _last_error = reason
    log.warning("MT5 disconnected: %s", reason)


def _is_terminal_alive() -> bool:
    """Real liveness probe, independent of the cached _connected flag.

    mt5.symbol_info_tick() (and the other data calls) silently return None
    when the underlying terminal process has died — they don't raise, and
    they never touch _connected. That left _connected stuck at True forever
    after an external terminal kill/restart, which meant _ensure_connected()
    kept skipping reconnection, _reconnect_loop() never saw a reason to act,
    and get_health() kept reporting connected=True — so the bridge watchdog
    (which only restarts/alerts when get_health() reports disconnected)
    never fired either. terminal_info() is a live round-trip to the
    terminal, so a None/disconnected result here means it's actually gone,
    not just a transient empty tick."""
    if not _MT5_AVAILABLE:
        return False
    try:
        info = mt5.terminal_info()
        return info is not None and bool(info.connected)
    except Exception:
        return False


def _disconnect():
    global _connected
    if _MT5_AVAILABLE:
        try:
            with _mt5_call_lock:
                mt5.shutdown()
        except Exception:
            pass
    with _lock:
        _connected = False


def _apply_credentials(login: int, password: str, server: str, terminal_path: str = "") -> dict:
    """Persist MT5 credentials and (re)connect with them. Shared by the
    HTTP /credentials handler and the in-process NativeMT5Bridge so there's
    exactly one implementation of "save + relogin" regardless of which
    process boundary is calling it."""
    global _connected, _last_error
    try:
        # Persist credentials (including terminal_path if supplied).
        creds_payload: dict = {"login": login, "password": password, "server": server}
        if terminal_path:
            creds_payload["terminal_path"] = terminal_path
        else:
            # Preserve existing terminal_path if already stored.
            existing_path = _load_credentials()[3]
            if existing_path:
                creds_payload["terminal_path"] = existing_path
        with open(CONFIG_PATH, "w") as f:
            json.dump(creds_payload, f)
        log.info("Credentials saved to %s for login %s", CONFIG_PATH, login)

        # Try login on the existing MT5 connection first — avoids a full
        # shutdown/initialize cycle which can launch a second terminal.
        ok = False
        with _lock:
            currently_connected = _connected

        if currently_connected and _MT5_AVAILABLE:
            try:
                auth = mt5.login(login=login, password=password, server=server)
                if auth:
                    with _lock:
                        _connected = True
                        _last_error = ""
                    info = mt5.account_info()
                    if info:
                        log.info(
                            "Re-logged in on existing connection. Login: %s, Server: %s",
                            info.login, info.server,
                        )
                    ok = True
            except Exception as login_exc:
                log.warning("Login on existing connection failed: %s — will full reconnect", login_exc)

        if not ok:
            # Full reconnect — disconnect, wait briefly, then reconnect.
            _disconnect()
            time.sleep(1)
            ok = _connect()

        trade_allowed    = None
        autotrading_info = None
        if ok and _MT5_AVAILABLE:
            # Enable AutoTrading immediately after login — MT5 resets it on
            # account switch and the user should not have to do this manually.
            autotrading_info = _try_enable_autotrading()
            trade_allowed    = autotrading_info.get("enabled", False)
            if not trade_allowed:
                log.warning(
                    "AutoTrading enable after login failed: %s",
                    autotrading_info.get("error"),
                )
        return {
            "status":        "connected" if ok else "credentials_saved_connect_failed",
            "error":         _last_error if not ok else None,
            "trade_allowed": trade_allowed,
            "autotrading":   autotrading_info,
        }
    except Exception as e:
        return {"error": str(e)}


def _try_enable_autotrading() -> dict:
    """Best-effort: click the AutoTrading button in the running MT5 terminal via Win32.
    Runs inside Wine Python so ctypes.windll is available.
    Returns {"enabled": bool, "method": str, "error": str|None}."""
    if not _MT5_AVAILABLE:
        return {"enabled": False, "error": "MT5 package not available"}
    try:
        term = mt5.terminal_info()
        if term is None:
            return {"enabled": False, "error": "terminal_info returned None — not connected"}
        if term.trade_allowed:
            return {"enabled": True, "method": "already_enabled"}
    except Exception as e:
        return {"enabled": False, "error": f"terminal_info error: {e}"}

    try:
        import ctypes
        user32 = ctypes.windll.user32

        # Enumerate all top-level windows and collect any whose title contains
        # "MetaTrader" — title matching is more reliable than class-name matching
        # across MT5 versions and Wine/CrossOver builds.
        #
        # IMPORTANT: The callback wrapper MUST be stored in a local variable before
        # being passed to EnumWindows — ctypes will GC a temporary immediately,
        # causing the callback to never fire (silent no-op enumeration).
        _found: list = []

        def _enum_cb(hwnd, _lp):
            _tbuf = ctypes.create_unicode_buffer(512)
            _cbuf = ctypes.create_unicode_buffer(256)
            nt = user32.GetWindowTextW(hwnd, _tbuf, 512)
            nc = user32.GetClassNameW(hwnd, _cbuf, 256)
            title = _tbuf.value if nt > 0 else ""
            cls   = _cbuf.value if nc > 0 else ""
            log.debug("Win32 EnumWindows: hwnd=0x%x cls=%r title=%r", hwnd, cls, title)
            if "MetaTrader" in title or "MetaQuotes" in cls or cls.startswith("Afx:"):
                _found.append((hwnd, title or cls))
            return True  # keep enumerating

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        _cb = WNDENUMPROC(_enum_cb)          # keep reference — prevents GC during enumeration
        user32.EnumWindows(_cb, 0)

        # Class-name fallback — try well-known MT5 class identifiers
        if not _found:
            for cls in ("MetaTrader5", "Afx:MetaTrader5", "MetaTrader 5",
                        "wndclass_desktopwnd", "CMainFrame"):
                h = user32.FindWindowW(cls, None)
                if h:
                    _found.append((h, f"<class:{cls}>"))
                    break

        if not _found:
            return {
                "enabled": False,
                "error": "MT5 main window not found — check that MT5 is running in CrossOver",
            }

        hwnd, wtitle = _found[0]
        log.info("AutoTrading: targeting window hwnd=0x%x title=%r", hwnd, wtitle)

        WM_COMMAND      = 0x0111
        VK_CONTROL      = 0x11
        VK_E            = 0x45
        KEYEVENTF_KEYUP = 0x0002

        def _check_enabled() -> bool:
            t = mt5.terminal_info()
            return bool(t and t.trade_allowed)

        # ── Attempt 1: WM_COMMAND 33070 (standard AutoTrading toolbar command) ──
        log.info("AutoTrading: PostMessage WM_COMMAND 33070 to hwnd=0x%x", hwnd)
        user32.PostMessageW(hwnd, WM_COMMAND, 33070, 0)
        time.sleep(1.5)
        if _check_enabled():
            return {"enabled": True, "method": "postmessage_33070"}

        # ── Attempt 2: Ctrl+E keyboard shortcut via keybd_event ──────────────
        # Bring MT5 to the foreground first so the keystroke reaches the right window
        log.info("AutoTrading: SetForegroundWindow + Ctrl+E keybd_event")
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        time.sleep(0.3)
        user32.keybd_event(VK_CONTROL, 0, 0, 0)            # Ctrl down
        user32.keybd_event(VK_E, 0, 0, 0)                  # E down
        time.sleep(0.05)
        user32.keybd_event(VK_E, 0, KEYEVENTF_KEYUP, 0)    # E up
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)  # Ctrl up
        time.sleep(1.5)
        if _check_enabled():
            return {"enabled": True, "method": "ctrl_e_keybd_event"}

        # ── Attempt 3: WM_COMMAND 33073 (seen in some MT5 locale variants) ───
        log.info("AutoTrading: PostMessage WM_COMMAND 33073 to hwnd=0x%x", hwnd)
        user32.PostMessageW(hwnd, WM_COMMAND, 33073, 0)
        time.sleep(1.5)
        if _check_enabled():
            return {"enabled": True, "method": "postmessage_33073"}

        return {
            "enabled": False,
            "error": (
                "Three enable attempts failed (WM_COMMAND 33070, Ctrl+E, WM_COMMAND 33073) — "
                "enable AutoTrading manually in the MT5 toolbar"
            ),
        }
    except Exception as e:
        log.warning("enable_autotrading win32 failed: %s", e)
        return {"enabled": False, "error": f"Win32 error: {e}"}


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _get_tick() -> dict | None:
    if not _ensure_connected():
        return None
    try:
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            if not _is_terminal_alive():
                _mark_disconnected("terminal_info() unreachable after empty tick — "
                                    "terminal likely restarted/closed externally")
            return None
        spread = round(tick.ask - tick.bid, 5)
        return {
            "symbol":        SYMBOL,
            "bid":           tick.bid,
            "ask":           tick.ask,
            "mid":           round((tick.bid + tick.ask) / 2, 5),
            "spread":        spread,
            "spread_points": round(spread / 0.01, 1),  # XAUUSD point = 0.01
            "last":          tick.last,
            "volume":        tick.volume,
            "timestamp":     tick.time,
            "source":        "mt5_vantage",
        }
    except Exception as e:
        log.warning("get_tick error: %s", e)
        return None


def _get_candles(timeframe: str, count: int) -> list[dict] | None:
    return _get_candles_for_symbol(SYMBOL, timeframe, count)


def _get_candles_range(from_ts: float, to_ts: float,
                       timeframe: str = "M1", symbol: str = SYMBOL) -> list[dict] | None:
    """Fetch candles between two Unix (true UTC) timestamps.

    Does NOT use mt5.copy_rates_range() — confirmed by direct testing
    (2026-07-07) that passing it UTC-derived naive datetimes returns candles
    from HOURS in the past relative to what was asked for, silently: MT5
    interprets the naive datetime using its own internal server-time
    convention, not UTC, and that offset isn't a fixed constant safe to
    hard-code since it also moves with the broker's own DST calendar. This
    was feeding wrong price data into _tp_safety_net_check_trade (bogus
    "extreme reached" determinations) and _max_tp_checker_loop (bogus
    max_tp_hit labels on closed trades).

    Fix: fetch by position instead (copy_rates_from_pos — no date
    interpretation at all, verified to return correct, current bars), then
    convert each bar's raw MT5 timestamp to true UTC using a freshly
    computed offset (this call's own live tick vs time.time()) and filter
    to the requested window. Self-correcting across any future DST shift
    since the offset is measured every call, never assumed.
    """
    if not _ensure_connected():
        return None
    tf = _TF_MAP.get(timeframe)
    if tf is None:
        return None
    try:
        tf_seconds = _TF_SECONDS.get(timeframe, 60)
        now = time.time()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        offset = tick.time - now  # add to true UTC to get MT5's raw .time convention

        bars_needed = max(int((now - from_ts) / tf_seconds) + 5, 5)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars_needed)
        if rates is None or len(rates) == 0:
            return []
        result = []
        for r in rates:
            true_ts = float(r[0]) - offset
            if from_ts - tf_seconds <= true_ts <= to_ts + tf_seconds:
                result.append({
                    "ts": int(true_ts), "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                })
        return result
    except Exception as e:
        log.warning("_get_candles_range error: %s", e)
        return None


# docs/todo/backtest/010 phase 1: one closed hour of XAUUSD ticks measured at
# 29,580-34,584 ticks / 1.7-2.0MB (2026-09-03 probe). A full day is tens of
# MB over this HTTP bridge, which already shows contention under concurrent
# native calls (see the _mt5_call_lock comment above) — bounded to a single
# calendar day per request so a careless range can't try to pull weeks of
# tick data through it.
_MAX_TICKS_RANGE_SEC = 86_400


def _get_ticks_range(from_ts: float, to_ts: float,
                     symbol: str = SYMBOL) -> list[dict] | None:
    """Fetch every tick between two Unix (true UTC) timestamps, bounded to
    one day.

    Unlike _get_candles_range, this uses mt5.copy_ticks_range() directly
    rather than working around a from-position + offset dance: the
    documented copy_rates_range() bug is a NAIVE-datetime interpretation
    problem, and passing tz-aware UTC datetimes here was confirmed correct
    against the live terminal by the 2026-09-03 probe (returned ticks landed
    exactly in the requested UTC window, not offset by the broker's server
    time). If that ever stops being true, this needs the same offset
    correction _get_candles_range applies.
    """
    if not _ensure_connected():
        return None
    if to_ts - from_ts > _MAX_TICKS_RANGE_SEC:
        return None
    try:
        start = datetime.fromtimestamp(float(from_ts), tz=timezone.utc)
        end   = datetime.fromtimestamp(float(to_ts), tz=timezone.utc)
        ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
        if ticks is None:
            return None
        return [
            {"time": float(t["time"]), "bid": float(t["bid"]), "ask": float(t["ask"])}
            for t in ticks
        ]
    except Exception as e:
        log.warning("_get_ticks_range error: %s", e)
        return None


def _get_candles_for_symbol(symbol: str, timeframe: str, count: int) -> list[dict] | None:
    if not _ensure_connected():
        return None
    tf = _TF_MAP.get(timeframe)
    if tf is None:
        return None
    try:
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        result = []
        for r in rates:
            result.append({
                "ts":     int(r[0]),
                "open":   float(r[1]),
                "high":   float(r[2]),
                "low":    float(r[3]),
                "close":  float(r[4]),
                "volume": float(r[5]),
            })
        return result
    except Exception as e:
        log.warning("get_candles_for_symbol(%s) error: %s", symbol, e)
        return None


def _get_account() -> dict | None:
    if not _ensure_connected():
        return None
    try:
        info = mt5.account_info()
        if info is None:
            return None
        trade_allowed = None
        try:
            term = mt5.terminal_info()
            trade_allowed = bool(term.trade_allowed) if term else None
        except Exception:
            pass
        return {
            "login":         info.login,
            "name":          info.name,
            "server":        info.server,
            "currency":      info.currency,
            "balance":       info.balance,
            "equity":        info.equity,
            "margin":        info.margin,
            "margin_free":   info.margin_free,
            "margin_level":  info.margin_level,
            "leverage":      info.leverage,
            "profit":        info.profit,
            "trade_mode":    info.trade_mode,
            "is_demo":       info.trade_mode == 0,  # 0 = demo in MT5
            "trade_allowed": trade_allowed,
        }
    except Exception as e:
        log.warning("get_account error: %s", e)
        return None


def _get_positions() -> list[dict] | None:
    if not _ensure_connected():
        return None
    try:
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions is None:
            # None always means an API error (empty set of positions is an
            # empty tuple/list, never None).  Mark disconnected so the engine
            # receives a 503 rather than an empty-list 200, which it cannot
            # distinguish from "genuinely no open positions".
            err = mt5.last_error()
            _mark_disconnected(f"positions_get() returned None: {err}")
            return None
        result = []
        for p in positions:
            result.append({
                "ticket":      p.ticket,
                "symbol":      p.symbol,
                "type":        "BUY" if p.type == 0 else "SELL",
                "volume":      p.volume,
                "open_price":  p.price_open,
                "current_price": p.price_current,
                "sl":          p.sl,
                "tp":          p.tp,
                "profit":      p.profit,
                "swap":        p.swap,
                "open_time":   p.time,
                "comment":     p.comment,
            })
        return result
    except Exception as e:
        log.warning("get_positions error: %s", e)
        return None


def _place_order(direction: str, lots: float, sl: float | None,
                  tp: float | None, comment: str = "VantageSimulator") -> dict:
    """Place a market order on the MT5 demo account. Returns ticket or error."""
    if not _ensure_connected():
        return {"error": f"Not connected: {_last_error}"}
    try:
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            return {"error": "No tick — symbol unavailable"}

        info = mt5.symbol_info(SYMBOL)
        if info is None:
            return {"error": "Symbol info unavailable"}

        # ── Pre-flight: check the symbol is currently open for trading ──────────
        trade_mode = getattr(info, "trade_mode", 4)
        _TRADE_MODE_NAMES = {
            0: "disabled",
            1: "long-only",
            2: "short-only",
            3: "close-only",
            4: "full",
        }
        if trade_mode == 0:
            return {"error": "Trading is disabled for XAUUSD on this account."}
        if trade_mode == 3:
            return {
                "error": (
                    "XAUUSD is in close-only mode. "
                    "The market is likely outside its trading hours — "
                    "XAUUSD trades Monday to Friday. Check the MT5 terminal for session times."
                ),
                "retcode": "market_closed",
            }

        # ── Pre-flight: AutoTrading must be enabled ──────────────────────────────
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            return {
                "error": (
                    "AutoTrading is disabled in MetaTrader 5. "
                    "Click the AutoTrading button (robot icon) in the MT5 toolbar to enable it, "
                    "then retry your trade."
                ),
                "retcode": 10027,
            }

        # Normalise lot to broker step
        lot_step = info.volume_step if info.volume_step else 0.01
        lots = round(round(lots / lot_step) * lot_step, 2)
        lots = max(info.volume_min, min(info.volume_max, lots))

        order_type = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        price      = tick.ask if direction.upper() == "BUY" else tick.bid

        # ── Pre-flight: broker minimum stop distance (fixes retcode 10016) ──────
        # trade_stops_level / trade_freeze_level are in points; SL/TP closer to
        # the current price than this are rejected as "Invalid stops". Scalp
        # stops sit near the limit, so pad them out to the minimum (plus one
        # point of headroom) instead of letting the whole order bounce.
        point    = getattr(info, "point", 0.01) or 0.01
        min_pts  = max(getattr(info, "trade_stops_level", 0) or 0,
                       getattr(info, "trade_freeze_level", 0) or 0)
        min_dist = (min_pts + 1) * point if min_pts > 0 else 0.0
        if min_dist > 0:
            if direction.upper() == "BUY":
                if sl and sl > tick.bid - min_dist:
                    sl = round(tick.bid - min_dist, 2)
                if tp and tp < tick.ask + min_dist:
                    tp = round(tick.ask + min_dist, 2)
            else:
                if sl and sl < tick.ask + min_dist:
                    sl = round(tick.ask + min_dist, 2)
                if tp and tp > tick.bid - min_dist:
                    tp = round(tick.bid - min_dist, 2)

        # Try all three filling modes — the broker's supported set is determined at
        # runtime.  Vantage Markets typically requires ORDER_FILLING_RETURN for
        # market orders; retcode 10030 means the mode was wrong, not that the order
        # itself is invalid, so we just try the next one.
        _FILL_MODES    = [mt5.ORDER_FILLING_RETURN,
                          mt5.ORDER_FILLING_IOC,
                          mt5.ORDER_FILLING_FOK]
        _FILL_ERRORS   = {10018, 10004, 10030}

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       lots,
            "type":         order_type,
            "price":        price,
            "deviation":    30,
            "magic":        20260601,
            "comment":      comment[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        if sl:
            request["sl"] = round(sl, 2)
        if tp:
            request["tp"] = round(tp, 2)

        result = None
        for filling in _FILL_MODES:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is None:
                break  # stage3/020: lost response != nothing filled (C3 double-fill)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return {
                    "success":     True,
                    "ticket":      result.order,
                    "fill_price":  result.price,
                    "volume":      result.volume,
                    "direction":   direction.upper(),
                    "sl":          sl,
                    "tp":          tp,
                }
            if result.retcode not in _FILL_ERRORS:
                break  # non-filling error — retrying with another mode won't help

        if result is None:  # unknown, NOT a rejection -- see stage3/020
            return {"error": f"order_send returned None: {mt5.last_error()}", "unknown": True}

        # Clear messages for known retcodes
        if result.retcode == 10027:
            return {
                "error": (
                    "AutoTrading is disabled in MetaTrader 5. "
                    "Click the AutoTrading button (robot icon) in the MT5 toolbar to enable it, "
                    "then retry your trade."
                ),
                "retcode": 10027,
            }
        if result.retcode == 10030:
            # retcode 10030 = TRADE_RETCODE_INVALID_FILL ("unsupported filling
            # mode") — NOT a market-hours error. We've already tried all three
            # filling modes above (RETURN/IOC/FOK) and every one was rejected
            # with this same code, which usually means the symbol is in a
            # state that rejects all order types right now (session gap,
            # rollover, or a broker-side restriction) rather than one
            # specific filling mode being wrong.
            return {
                "error": (
                    "XAUUSD rejected the order under every supported filling mode "
                    "(retcode 10030 on RETURN, IOC, and FOK). This usually means the "
                    "symbol isn't accepting any new orders right now — check the MT5 "
                    "terminal for the current session state (this can happen during a "
                    "session gap or broker rollover even within normal trading hours) "
                    "rather than assuming the market is simply closed."
                ),
                "retcode": 10030,
            }
        if result.retcode == 10018:
            return {
                "error": "Market is closed or not accepting new orders for XAUUSD at this time.",
                "retcode": 10018,
            }
        return {"error": f"Order failed retcode={result.retcode}: {result.comment}",
                "retcode": result.retcode}
    except Exception as e:
        log.exception("_place_order error")
        return {"error": str(e)}


def _close_position(ticket: int) -> dict:
    """Close an open position by ticket number."""
    if not _ensure_connected():
        return {"error": _last_error}
    try:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"error": f"Position {ticket} not found"}
        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return {"error": "No tick"}
        close_type  = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    30,
            "magic":        20260601,
            "comment":      "close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "ticket": ticket, "close_price": close_price}
        return {"error": f"Close failed retcode={result.retcode if result else '?'}: "
                         f"{result.comment if result else mt5.last_error()}"}
    except Exception as e:
        return {"error": str(e)}


def _partial_close(ticket: int, lots: float) -> dict:
    """Partially close an open position."""
    if not _ensure_connected():
        return {"error": _last_error}
    try:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"error": f"Position {ticket} not found"}
        pos  = positions[0]
        info = mt5.symbol_info(pos.symbol)
        lot_step = info.volume_step if info else 0.01
        lots = round(round(lots / lot_step) * lot_step, 2)
        lots = max(lot_step, min(pos.volume, lots))
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return {"error": "No tick"}
        close_type  = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       lots,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    30,
            "magic":        20260601,
            "comment":      "partial",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "ticket": ticket, "lots_closed": lots,
                    "close_price": close_price, "remaining": round(pos.volume - lots, 2)}
        return {"error": f"Partial close failed: {result.retcode if result else '?'}"}
    except Exception as e:
        return {"error": str(e)}


def _modify_position(ticket: int, sl: float | None, tp: float | None) -> dict:
    """Modify SL/TP on an existing position."""
    if not _ensure_connected():
        return {"error": _last_error}
    try:
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"error": f"Position {ticket} not found"}
        pos = positions[0]

        # Clamp to the broker minimum stop distance (same guard as _place_order;
        # break-even SL moves land close to price and are the main 10016 source).
        info = mt5.symbol_info(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        if info is not None and tick is not None:
            point   = getattr(info, "point", 0.01) or 0.01
            min_pts = max(getattr(info, "trade_stops_level", 0) or 0,
                          getattr(info, "trade_freeze_level", 0) or 0)
            min_dist = (min_pts + 1) * point if min_pts > 0 else 0.0
            if min_dist > 0:
                if pos.type == 0:  # BUY position — closes at bid
                    if sl and sl > tick.bid - min_dist:
                        sl = round(tick.bid - min_dist, 2)
                    if tp and tp < tick.bid + min_dist:
                        tp = round(tick.bid + min_dist, 2)
                else:              # SELL position — closes at ask
                    if sl and sl < tick.ask + min_dist:
                        sl = round(tick.ask + min_dist, 2)
                    if tp and tp > tick.ask - min_dist:
                        tp = round(tick.ask - min_dist, 2)

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   pos.symbol,
            "sl":       round(sl, 2) if sl else pos.sl,
            "tp":       round(tp, 2) if tp else pos.tp,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"success": True, "ticket": ticket}
        return {"error": f"Modify failed: {result.retcode if result else '?'}"}
    except Exception as e:
        return {"error": str(e)}


def _get_history_by_position(position_id: int) -> list[dict] | None:
    """
    Query deal history for a SPECIFIC position ticket.
    Uses mt5.history_deals_get(position=...) which queries MT5's live deal
    database directly — it is NOT subject to the date-range caching that
    affects the time-window variant and always returns the current state.
    """
    if not _ensure_connected():
        return None
    try:
        deals = mt5.history_deals_get(position=position_id)
        if deals is None:
            return []
        result = []
        for d in deals:
            result.append({
                "ticket":      d.ticket,
                "order":       d.order,
                "position_id": d.position_id,
                "entry":       d.entry,
                "symbol":      d.symbol,
                "type":        d.type,
                "volume":      d.volume,
                "price":       d.price,
                "profit":      d.profit,
                "swap":        d.swap,
                "fee":         d.fee,
                "time":        d.time,
                "comment":     d.comment,
            })
        return result
    except Exception as e:
        log.warning("get_history_by_position error: %s", e)
        return None


def _get_history(days: int, flush: bool = False) -> list[dict] | None:
    if not _ensure_connected():
        return None
    try:
        if flush:
            _ = mt5.terminal_info()   # lightweight session-state refresh

        from_ts = int(time.time()) - (days * 86400)
        from_dt = datetime.fromtimestamp(from_ts, tz=timezone.utc)
        # Extend to_dt by 24 h so deals whose broker-server timestamp is ahead of local
        # UTC (e.g. UTC+3 broker = 3 h ahead) are not excluded from the result set.
        to_dt   = datetime.now(tz=timezone.utc) + timedelta(hours=24)
        # Fetch ALL deals (no group filter) — the group= parameter silently returns
        # nothing on some broker configurations even when deals exist.  Filter by
        # symbol in Python instead.
        deals = mt5.history_deals_get(from_dt, to_dt)
        if deals is None:
            return []
        result = []
        for d in deals:
            if d.symbol and d.symbol != SYMBOL:
                continue
            result.append({
                "ticket":      d.ticket,
                "order":       d.order,
                "position_id": d.position_id,  # position this deal belongs to
                "entry":       d.entry,         # 0=IN(open) 1=OUT(close) 2=INOUT
                "symbol":      d.symbol,
                "type":        d.type,
                "volume":      d.volume,
                "price":       d.price,
                "profit":      d.profit,
                "swap":        d.swap,
                "fee":         d.fee,
                "time":        d.time,
                "comment":     d.comment,
            })
        return result
    except Exception as e:
        log.warning("get_history error: %s", e)
        return None


def _get_tick_at(ts: float) -> dict | None:
    """
    Nearest real bid/ask at or after a historical timestamp — used to compute
    the actual spread paid on a past trade (copy_ticks_from returns ticks
    starting at the given time, not before it, which is fine for this purpose
    since the deal's own fill happened at-or-after this moment).
    """
    if not _ensure_connected():
        return None
    try:
        from_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        ticks = mt5.copy_ticks_from(SYMBOL, from_dt, 1, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return None
        t = ticks[0]
        bid, ask = float(t["bid"]), float(t["ask"])
        spread = round(ask - bid, 5)
        return {
            "bid": bid, "ask": ask, "spread": spread,
            "spread_points": round(spread / 0.01, 1),
            "time": int(t["time"]),
        }
    except Exception as e:
        log.warning("get_tick_at error: %s", e)
        return None


# ── HTTP handler ──────────────────────────────────────────────────────────────

# The MetaTrader5 Python package wraps a single IPC connection to the
# terminal; MetaQuotes does not document it as safe for concurrent calls
# from multiple threads. ThreadingHTTPServer (below) lets socket accept/read/
# write for different requests overlap instead of queuing strictly one at a
# time behind whichever request is currently running — that queuing was the
# actual cause of the app's bridge_watchdog occasionally timing out a
# /health check that was sitting behind an unrelated slow /candles request,
# with MT5 itself perfectly healthy. This lock keeps the *native calls*
# fully serialized (safe) while removing that queuing bottleneck at the
# HTTP layer (fast).
#
# NOTE: wrapping every handler in this single lock still lets a genuinely
# slow native call (e.g. /history?days=365, which walks a full year of
# broker deals) block *every other* request behind it — including /tick,
# which the app polls every ~250ms-1s and treats as a hard failure after a
# 4s client timeout. /tick and /health are pulled out from behind this lock
# below (tick via a background pump + cache, health via a bounded-wait
# acquire) so a slow /history call can no longer starve them.
_mt5_call_lock = threading.Lock()

_tick_cache: dict | None = None
_tick_cache_ts: float = 0.0
_TICK_PUMP_INTERVAL  = 0.25  # seconds between native tick refreshes
_TICK_CACHE_MAX_AGE  = 2.0   # serve cached tick up to this stale before refusing


def _tick_pump_loop() -> None:
    """Background thread: refreshes the tick cache under the lock on its own
    cadence, decoupled from HTTP request timing, so /tick requests can be
    served from cache without ever waiting on a slow /history or /candles call."""
    global _tick_cache, _tick_cache_ts
    while True:
        try:
            with _mt5_call_lock:
                t = _get_tick()
            if t:
                _tick_cache = t
                _tick_cache_ts = time.time()
        except Exception as e:
            log.debug("tick pump error: %s", e)
        time.sleep(_TICK_PUMP_INTERVAL)


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access logs

    def _send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg: str, status: int = 503):
        self._send_json({"error": msg, "connected": _connected}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        if path in (f"/tick/{SYMBOL}", "/tick"):
            self._serve_tick_from_cache()
            return
        if path == "/health":
            self._serve_health_bounded()
            return
        with _mt5_call_lock:
            self._do_GET_locked()

    def _serve_tick_from_cache(self) -> None:
        if _tick_cache and (time.time() - _tick_cache_ts) < _TICK_CACHE_MAX_AGE:
            self._send_json(_tick_cache)
            return
        # Pump hasn't produced fresh data yet (e.g. just started) — fall back
        # to a direct locked call rather than serving nothing.
        with _mt5_call_lock:
            tick = _get_tick()
        if tick:
            self._send_json(tick)
        else:
            self._send_error(f"No tick data. Connected={_connected}. {_last_error}")

    def _serve_health_bounded(self) -> None:
        # /health drives the app's bridge watchdog — it must never queue
        # behind a slow /history or /candles call. Try briefly for a fresh
        # terminal_info() read; if the lock is held by something slow, fall
        # back to the last-known connection state instead of blocking.
        acquired = _mt5_call_lock.acquire(timeout=1.0)
        try:
            with _lock:
                connected = _connected
                error     = _last_error
                ct        = _connect_time
            trade_allowed = None
            if acquired and connected and _MT5_AVAILABLE:
                try:
                    term = mt5.terminal_info()
                    if term is None:
                        # MT5 terminal has closed or become unreachable
                        _mark_disconnected("terminal_info() returned None — MT5 may have closed")
                        connected = False
                        with _lock:
                            error = _last_error
                    else:
                        trade_allowed = bool(term.trade_allowed)
                except Exception as e:
                    _mark_disconnected(f"health check error: {e}")
                    connected = False
                    with _lock:
                        error = _last_error
        finally:
            if acquired:
                _mt5_call_lock.release()
        self._send_json({
            "status":        "connected" if connected else "disconnected",
            "connected":     connected,
            "mt5_available": _MT5_AVAILABLE,
            "symbol":        SYMBOL,
            "last_error":    error,
            "connect_time":  ct,
            "trade_allowed": trade_allowed,
        })

    def _do_GET_locked(self):
        # /health and /tick are handled in do_GET before this lock is ever
        # acquired — see _serve_health_bounded / _serve_tick_from_cache.
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path.startswith("/candles_symbol/"):
            symbol_req = path.split("/candles_symbol/", 1)[1].split("?")[0].strip() or SYMBOL
            tf    = params.get("timeframe", ["M5"])[0]
            count = int(params.get("count", ["50"])[0])
            count = min(max(count, 5), 50000)
            candles = _get_candles_for_symbol(symbol_req, tf, count)
            if candles is not None:
                self._send_json({"symbol": symbol_req, "timeframe": tf,
                                 "count": len(candles), "candles": candles})
            else:
                self._send_error(f"No candle data for {symbol_req}.")

        elif path == "/candles_range":
            tf      = params.get("timeframe", ["M1"])[0]
            from_ts = float(params.get("from", ["0"])[0])
            to_ts   = float(params.get("to",   ["0"])[0])
            symbol_req = params.get("symbol", [SYMBOL])[0]
            if not from_ts or not to_ts or to_ts <= from_ts:
                self._send_error("candles_range requires from= and to= Unix timestamps")
            else:
                candles = _get_candles_range(from_ts, to_ts, tf, symbol_req)
                if candles is not None:
                    self._send_json({"timeframe": tf, "count": len(candles), "candles": candles})
                else:
                    self._send_error(f"No range candle data. Connected={_connected}.")

        elif path == "/ticks":
            from_ts = float(params.get("from", ["0"])[0])
            to_ts   = float(params.get("to",   ["0"])[0])
            symbol_req = params.get("symbol", [SYMBOL])[0]
            if not from_ts or not to_ts or to_ts <= from_ts:
                self._send_error("ticks requires from= and to= Unix timestamps")
            elif to_ts - from_ts > _MAX_TICKS_RANGE_SEC:
                self._send_error(f"range exceeds the {_MAX_TICKS_RANGE_SEC}s (1 day) limit")
            else:
                ticks = _get_ticks_range(from_ts, to_ts, symbol_req)
                if ticks is not None:
                    self._send_json({"count": len(ticks), "ticks": ticks})
                else:
                    self._send_error(f"No tick data. Connected={_connected}.")

        elif path.startswith("/candles"):
            tf    = params.get("timeframe", ["M5"])[0]
            count = int(params.get("count", ["200"])[0])
            count = min(max(count, 10), 50000)
            candles = _get_candles(tf, count)
            if candles is not None:
                self._send_json({"timeframe": tf, "count": len(candles), "candles": candles})
            else:
                self._send_error(f"No candle data. Connected={_connected}. {_last_error}")

        elif path == "/account":
            acc = _get_account()
            if acc:
                self._send_json(acc)
            else:
                self._send_error(f"No account data. Connected={_connected}. {_last_error}")

        elif path == "/positions":
            pos = _get_positions()
            if pos is not None:
                self._send_json({"positions": pos})
            else:
                self._send_error(f"No position data. Connected={_connected}. {_last_error}")

        elif path == "/history":
            days  = int(params.get("days",  ["7"])[0])
            flush = params.get("flush", ["0"])[0] in ("1", "true", "yes")
            hist  = _get_history(days, flush=flush)
            if hist is not None:
                self._send_json({"history": hist})
            else:
                self._send_error(f"No history data. Connected={_connected}. {_last_error}")

        elif path.startswith("/history/position/"):
            # Position-specific deal query — bypasses date-range caching
            try:
                pos_id = int(path.split("/")[-1])
                hist   = _get_history_by_position(pos_id)
                if hist is not None:
                    self._send_json({"history": hist, "position_id": pos_id})
                else:
                    self._send_error(f"No history data for position {pos_id}.")
            except (ValueError, IndexError):
                self._send_json({"error": "Invalid position ID"}, 400)

        elif path == "/tick_at":
            try:
                ts = float(params.get("ts", ["0"])[0])
                tick = _get_tick_at(ts)
                if tick is not None:
                    self._send_json(tick)
                else:
                    self._send_error(f"No tick data at ts={ts}.")
            except (ValueError, IndexError):
                self._send_json({"error": "Invalid ts"}, 400)

        else:
            self._send_json({"error": "Unknown path"}, 404)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):
        with _mt5_call_lock:
            self._do_POST_locked()

    def _do_POST_locked(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path == "/credentials":
            body = self._read_body()
            login         = int(body.get("login", 0))
            password      = body.get("password", "")
            server        = body.get("server", "")
            terminal_path = body.get("terminal_path", "") or ""
            if not login or not password or not server:
                self._send_json({"error": "login, password and server are required"}, 400)
                return
            self._send_json(_apply_credentials(login, password, server, terminal_path))

        elif path == "/reconnect":
            _disconnect()
            time.sleep(1)   # give the terminal IPC time to release before re-init
            ok = _connect()
            self._send_json({"status": "connected" if ok else "failed", "error": _last_error})

        elif path == "/enable_autotrading":
            self._send_json(_try_enable_autotrading())

        elif path == "/order":
            body = self._read_body()
            result = _place_order(
                direction = body.get("direction", "BUY"),
                lots      = float(body.get("lots", 0.01)),
                sl        = body.get("sl"),
                tp        = body.get("tp"),
                comment   = body.get("comment", "VantageSimulator"),
            )
            status = 200 if result.get("success") else 400
            self._send_json(result, status)

        elif path.startswith("/close/"):
            ticket = int(path.split("/")[-1])
            self._send_json(_close_position(ticket))

        elif path.startswith("/partial-close/"):
            ticket = int(path.split("/")[-1])
            body   = self._read_body()
            self._send_json(_partial_close(ticket, float(body.get("lots", 0.01))))

        elif path.startswith("/modify/"):
            ticket = int(path.split("/")[-1])
            body   = self._read_body()
            self._send_json(_modify_position(ticket, body.get("sl"), body.get("tp")))

        else:
            self._send_json({"error": "Unknown path"}, 404)


# ── Background reconnect loop ─────────────────────────────────────────────────

def _reconnect_loop():
    while True:
        time.sleep(RECONNECT_EVERY)
        with _lock:
            connected = _connected
        if not connected:
            log.info("Attempting reconnect...")
            _connect()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not _MT5_AVAILABLE:
        log.error("Cannot start: MetaTrader5 Python package is not installed.")
        log.error("Run setup_wine_bridge.sh to install it inside Wine Python.")
        sys.exit(1)

    log.info("MT5 Bridge starting on port %d", PORT)
    log.info("Symbol: %s", SYMBOL)

    # Initial connection attempt
    if not _connect():
        log.warning("Initial MT5 connection failed. Will retry every %ds.", RECONNECT_EVERY)
        log.warning("Make sure the MetaTrader 5 terminal is running.")

    # Background reconnect
    t = threading.Thread(target=_reconnect_loop, daemon=True)
    t.start()

    # Background tick pump — keeps the /tick cache fresh independent of
    # HTTP request timing, so a slow /history call can't starve tick polling.
    tp = threading.Thread(target=_tick_pump_loop, daemon=True)
    tp.start()

    # Start HTTP server. Threaded so multiple engines' concurrent requests
    # don't queue behind each other at the socket level — see _mt5_call_lock
    # above for how the actual MT5 API calls stay safely serialized.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), BridgeHandler)
    server.daemon_threads = True
    log.info("Bridge listening on http://127.0.0.1:%d", PORT)
    log.info("Service expects MT5_BRIDGE_URL=http://host.docker.internal:%d in docker-compose", PORT)
    log.info("  or MT5_BRIDGE_URL=http://localhost:%d if running outside Docker", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down bridge.")
        _disconnect()


if __name__ == "__main__":
    main()
