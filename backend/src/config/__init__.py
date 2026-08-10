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
# This checkout (forex-refactor2) is a full fork of the live, actively-trading
# app at /Users/simon/Documents/FOREX -- it must NEVER default to that app's
# "ForexTrader" folder, or a dev run here would boot against the live app's
# real MT5 bridge, Telegram session, and remote-sync state (confirmed live
# 2026-07-21: a smoke-test run reconnected the live Telegram session, wrote a
# real signal row to the shared reversal_engine.db, and started the shared
# remote-admin server, all before this fix existed). "ForexTrader-Refactor2"
# is a separate, genuinely empty folder by default -- override with the
# FOREX_TRADER_DATA_DIR env var if a different location is ever needed.
_APP_DATA_FOLDER = os.environ.get("FOREX_TRADER_DATA_DIR_NAME", "ForexTrader-Refactor2")
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
        # 8888 is the live app's UI port -- run.py's _free_port() kills
        # whatever's already listening on this port before starting, so
        # defaulting to 8888 here would kill the live app's web server.
        "port": int(_e("PORT", base.get("port", 8890))),

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
        # node that can receive pushed software updates. This checkout is an
        # isolated fork with no business joining that fleet -- default off
        # here; the live app's own config.yaml (which never sets this key)
        # keeps its unrelated True default via base.get() below.
        "remote_admin_client_enabled": str(_e(
            "REMOTE_ADMIN_CLIENT_ENABLED", base.get("remote_admin_client_enabled", False)
        )).lower() == "true",
    }

    # Derive DB path from account_env (must be after the dict is partially built)
    env     = _cfg["account_env"]
    db_name = f"forex_trader_{env}.db"
    _cfg["db_path"]      = str(DATA_DIR / db_name)
    _cfg["sessions_dir"] = str(SESSIONS_DIR)
    return _cfg


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
