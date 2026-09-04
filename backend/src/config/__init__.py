"""
Configuration loader.
Reads config.yaml, then applies environment variable overrides.
All code should call config.get() to access settings.
"""

import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

BASE_DIR = Path(__file__).parent.parent  # project root — code only

# All user data (config, databases, sessions, logs) lives outside the project
# so that distributing a new version of the app never touches user credentials.
# Path is platform-specific; all downstream code uses this constant.
#
# History, because the value here has flipped twice and the reasoning matters:
#
# This checkout began as a fork of a separate, live, actively-trading app, and
# defaulting to that app's "ForexTrader" folder was genuinely dangerous --
# confirmed live 2026-07-21, when a smoke-test run reconnected the LIVE
# Telegram session, wrote a real signal row to the shared reversal_engine.db
# and started the shared remote-admin server. So the default became
# "ForexTrader-Refactor2".
#
# That isolation need ended on 2026-07-24: the fork was promoted to be the only
# app and the original was retired. The separate name then became its own bug --
# every launcher and the installer's [Dirs] section still said "ForexTrader"
# while the running code wrote to "ForexTrader-Refactor2", which stranded a
# config.yaml on the Windows client. Upstream reverted it (212fd87) and the
# 2026-08-25 merge takes that revert: "ForexTrader" is correct.
#
# The 2026-07-21 hazard is not gone, only narrowed: if you ever run this
# checkout alongside another install on one machine, set FOREX_TRADER_DATA_DIR
# (or FOREX_TRADER_DATA_DIR_NAME) FIRST. Nothing in the code can tell the two
# apart -- the only thing standing between a dev run and the live database is
# that there is currently only one app.
_APP_DATA_FOLDER = os.environ.get("FOREX_TRADER_DATA_DIR_NAME", "ForexTrader")
if sys.platform == "win32":
    USER_DATA_DIR = Path.home() / "AppData" / "Roaming" / _APP_DATA_FOLDER
elif sys.platform == "darwin":
    USER_DATA_DIR = Path.home() / "Library" / "Application Support" / _APP_DATA_FOLDER
else:
    USER_DATA_DIR = Path.home() / ".config" / _APP_DATA_FOLDER
if os.environ.get("FOREX_TRADER_DATA_DIR"):
    USER_DATA_DIR = Path(os.environ["FOREX_TRADER_DATA_DIR"])
CONFIG_FILE   = USER_DATA_DIR / "config.yaml"
DATA_DIR      = USER_DATA_DIR / "data"
SESSIONS_DIR  = DATA_DIR / "sessions"
# DB_PATH is resolved dynamically in load() based on account_env
DB_PATH = DATA_DIR / "forex_trader_demo.db"  # fallback used only before load()

_cfg: dict = {}


def _load_yaml() -> dict:
    if not _YAML_AVAILABLE or not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# Legacy Claude model IDs that may be stored in config.yaml from older installs.
# Resolved at load time so no UI code ever receives an invalid value.
_CLAUDE_ALIASES: dict[str, str] = {
    "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5": "claude-sonnet-4-6",
    "claude-opus-4-5":   "claude-opus-4-8",
}


def load() -> dict:
    global _cfg
    base = _load_yaml()

    def _e(key: str, default: Any = "") -> Any:
        return os.environ.get(key) or base.get(key.lower()) or default

    def _claude_model() -> str:
        raw = _e("CLAUDE_MODEL", "claude-sonnet-4-6")
        return _CLAUDE_ALIASES.get(raw, raw)

    _cfg = {
        # MT5
        "mt5_login":          int(_e("MT5_LOGIN", base.get("mt5_login", 0))),
        "mt5_server":         _e("MT5_SERVER", ""),
        # Port 9000 is the LIVE app's bridge port (mt5_bridge.py under Wine,
        # always running) -- defaulting there would let this fork's engine
        # silently query (and, if auto-execute is ever enabled, trade on)
        # the real account through the live app's own bridge. 9010 is a
        # separate port nothing listens on by default, so an unconfigured
        # fork's bridge client gets connection-refused and the engine falls
        # back to its normal "bridge not configured" behavior instead.
        "mt5_bridge_url":     _e("MT5_BRIDGE_URL", "http://localhost:9010"),
        "mt5_bridge_enabled": str(_e("MT5_BRIDGE_ENABLED", base.get("mt5_bridge_enabled", True))).lower() != "false",
        # Port 9101 is the LIVE app's EA-bridge listener (core/ea_bridge.py) --
        # the companion MQL5 EA's InpPort input must match whatever this
        # resolves to. 9111 by default here so a fork EA never accidentally
        # dials into the live app's bridge (or vice versa) on a shared machine.
        "ea_bridge_port":     int(_e("EA_BRIDGE_PORT", base.get("ea_bridge_port", 9111))),
        # Native Windows can import MetaTrader5 directly in the main process
        # instead of running mt5_bridge.py as a separate HTTP-served
        # subprocess (that split only exists because macOS runs MT5 under
        # Wine, where the main app's own Python can't import MetaTrader5).
        # Defaults on for win32; an escape hatch in case something doesn't
        # behave the same in-process as it does over the bridge, without
        # needing a code rollback to fall back to the old subprocess+HTTP path.
        "mt5_native_bridge_enabled": str(_e(
            "MT5_NATIVE_BRIDGE_ENABLED", base.get("mt5_native_bridge_enabled", True)
        )).lower() != "false",

        # Wine bridge paths (macOS).
        # wine_bin: the Wine binary to use — CrossOver's binary is the default because it
        #   ships with better Apple Silicon support.  After installing a system Wine via
        #   Homebrew, update this to e.g. /opt/homebrew/bin/wine.
        # mt5_bottle_path: the WINEPREFIX directory.  After running setup_wine_bridge.sh
        #   this is ~/.wine_mt5 (independent of CrossOver).
        "wine_bin": _e(
            "WINE_BIN",
            base.get("wine_bin",
                      "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"),
        ),
        "mt5_bottle_path": _e(
            "MT5_BOTTLE_PATH",
            base.get("mt5_bottle_path",
                      os.path.expanduser("~/.wine_mt5")),
        ),
        # "crossover" = use CrossOver-managed bottle (recommended, known working)
        # "wine"      = use an independent WINEPREFIX at mt5_bottle_path
        "bridge_backend": _e(
            "BRIDGE_BACKEND",
            base.get("bridge_backend", "crossover"),
        ),

        # Simulation
        "starting_balance":   float(_e("STARTING_BALANCE", base.get("starting_balance", 1000.0))),

        # AI
        "ai_provider":        _e("AI_PROVIDER", base.get("ai_provider", "claude")),  # "claude" | "deepseek"
        "anthropic_api_key":  _e("ANTHROPIC_API_KEY", ""),
        "claude_model":       _claude_model(),
        "deepseek_api_key":   _e("DEEPSEEK_API_KEY", ""),
        "deepseek_model":     _e("DEEPSEEK_MODEL", base.get("deepseek_model", "deepseek-v4-flash")),
        # Cached model lists for the Settings > AI dropdowns, refreshed daily by
        # SimulationEngine._ai_model_refresh_loop() (and on-demand via a
        # "Refresh models" button) — never queried live on every page render.
        "claude_models_cache":     base.get("claude_models_cache", []),
        "deepseek_models_cache":   base.get("deepseek_models_cache", []),
        "ai_models_last_refreshed": float(base.get("ai_models_last_refreshed", 0)),

        # Telegram reader (Telethon)
        "telegram_api_id":          _e("TELEGRAM_API_ID", ""),
        "telegram_api_hash":        _e("TELEGRAM_API_HASH", ""),
        "telegram_phone":           _e("TELEGRAM_PHONE_NUMBER", base.get("telegram_phone", "")),
        "telegram_2fa_password":    _e("TELEGRAM_2FA_PASSWORD", base.get("telegram_2fa_password", "")),
        "telegram_session_name":    _e("TELEGRAM_SESSION_NAME", base.get("telegram_session_name", "forex_trader")),
        "telegram_signal_group_id": str(_e("TELEGRAM_SIGNAL_GROUP_ID", base.get("telegram_signal_group_id", ""))),

        # Telegram bot alerts
        "telegram_bot_token": _e("TELEGRAM_BOT_TOKEN", base.get("telegram_bot_token", "")),
        "telegram_chat_id":   _e("TELEGRAM_CHAT_ID", base.get("telegram_chat_id", "")),

        # App
        "port": int(_e("PORT", base.get("port", 8888))),

        # Settings > Security: does a restart ask for the dashboard password.
        # It has to be named here. load() rebuilds _cfg from this literal, so a
        # key that only ever reaches config.yaml through save_to_yaml() is
        # dropped by the reload save_to_yaml() itself performs -- the setting
        # wrote to disk correctly and read back as absent, which is exactly
        # what the owner saw on 2026-09-04 (chose "Log in automatically",
        # saved, came back to "Ask for the dashboard password").
        # Default False: an install that has never set it keeps asking.
        "auto_login_enabled": str(_e("AUTO_LOGIN_ENABLED", False)).lower()
                              in ("1", "true", "yes"),

        # Settings > News: the blackout that refuses automated entries around
        # economic releases. Same story as auto_login_enabled above -- the page
        # wrote all four keys and load() named none of them, so every reader
        # got news_calendar's hardcoded defaults instead. `enabled` defaults
        # True, so switching the blackout OFF in the UI did nothing at all;
        # found 2026-09-04, on by default the whole time.
        #
        # NOT via _e(): it resolves with `or`, so a saved False or a saved 0
        # is falsy and falls through to the default -- which is the exact bug
        # this block exists to fix. Read env-then-yaml-then-default by hand.
        # Defaults mirror news_calendar._DEF_* (config imports nothing, so they
        # cannot be shared); clamping stays in news_calendar.
        "news_blackout_enabled": str(os.environ.get(
            "NEWS_BLACKOUT_ENABLED", base.get("news_blackout_enabled", True)
        )).strip().lower() not in ("0", "false", "no"),
        "news_blackout_impact": str(os.environ.get(
            "NEWS_BLACKOUT_IMPACT", base.get("news_blackout_impact", "high")
        )).strip().lower(),
        "news_blackout_minutes_before": int(os.environ.get(
            "NEWS_BLACKOUT_MINUTES_BEFORE",
            base.get("news_blackout_minutes_before", 30),
        )),
        "news_blackout_minutes_after": int(os.environ.get(
            "NEWS_BLACKOUT_MINUTES_AFTER",
            base.get("news_blackout_minutes_after", 30),
        )),

        # Debug mode: run the whole app on fakes with no credentials or network
        # (fake MT5 bridge, fake Telegram reader, canned news/AI/email). Default
        # OFF; env FOREX_DEBUG_MODE wins over config.yaml's debug_mode:. When on,
        # the DB is isolated to its own file (below) so a debug boot can never
        # open a demo/live database. Read explicitly (not via _e) because the env
        # name is FOREX_DEBUG_MODE while the yaml key is debug_mode.
        "debug_mode": (
            os.environ.get("FOREX_DEBUG_MODE") or str(base.get("debug_mode", False))
        ).strip().lower() in ("1", "true", "yes"),

        # Bind address for the dashboard web server. Defaults to loopback:
        # the UI can place and close live orders and has no login of its own,
        # so it must not be reachable from other machines. Widen this (to
        # 0.0.0.0 or a LAN IP) only once real authentication is in front of it
        # -- run.py logs a warning when a non-loopback host is configured.
        "host": _e("HOST", base.get("host", "127.0.0.1")),

        # Environment: "demo" or "live" — controls which DB file is used
        "account_env":   _e("ACCOUNT_ENV", base.get("account_env", "demo")),

        # Remote-admin fleet client (forex_trader/remote/client.py): connects
        # out to the live app's real admin server (217.155.25.160:8443) and
        # LAN-scans for a beacon, reporting this machine in as a manageable
        # node the admin console can see, licence and update.
        #
        # Default ON since 2026-08-26 (Q001 #5, amended). It was off because
        # this checkout was an isolated fork with no business joining the
        # fleet, AND because the channel then applied pushed CODE with no
        # signature check. Both premises changed: the fork was promoted to be
        # the only app, and upstream 0815cc6 deleted the zip-push entirely --
        # a push now only asks the client to run its own git pull. Simon uses
        # the console for licence permissions and to see who is online, so a
        # client that never connects is a broken feature, not a safe default.
        #
        # What is still NOT authenticated: the TLS connection runs CERT_NONE
        # with no certificate pinning (remote/tls.py), so someone on the
        # network path can still impersonate the admin server. Cert pinning is
        # the tracked follow-up; see docs/simon-handover/001-trading-defaults.md.
        "remote_admin_client_enabled": str(_e(
            "REMOTE_ADMIN_CLIENT_ENABLED", base.get("remote_admin_client_enabled", True)
        )).lower() == "true",
    }

    # Derive DB path from account_env (must be after the dict is partially built).
    # Debug mode always gets its OWN file, independent of account_env, so a debug
    # boot can never read or write a real demo/live database.
    if _cfg["debug_mode"]:
        db_name = "forex_trader_debug.db"
    else:
        db_name = f"forex_trader_{_cfg['account_env']}.db"
    _cfg["db_path"]      = str(DATA_DIR / db_name)
    _cfg["sessions_dir"] = str(SESSIONS_DIR)
    return _cfg


def is_debug() -> bool:
    """True when the app is running on fakes (debug mode). Reads the loaded
    config; call load() first (run.py and app.startup() both do)."""
    return bool((_cfg or {}).get("debug_mode", False))


def get(key: str, default: Any = None) -> Any:
    if not _cfg:
        load()
    return _cfg.get(key, default)


def reload() -> dict:
    """Re-read config.yaml and env vars. Useful after in-app settings save."""
    return load()


def save_to_yaml(updates: dict) -> None:
    """Persist a subset of settings back to config.yaml."""
    current = _load_yaml()
    current.update(updates)
    if _YAML_AVAILABLE:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(current, f, default_flow_style=False, allow_unicode=True)
        reload()
