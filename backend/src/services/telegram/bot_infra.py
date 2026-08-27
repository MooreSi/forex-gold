"""Infrastructure/process Telegram bot commands -- extracted verbatim (no
logic changes) from core/engine.py's SimulationEngine._cmd_restart_bridge/
_cmd_restart_app/_cmd_headless/_cmd_switch_live/_cmd_switch_demo/
_cmd_switch_env, as part of the core/engine.py migration series. See
docs/todo/refactor/core-bot-commands-infra-migration/020-*.md.

No bridge order calls anywhere in this module. `_cmd_switch_env` repoints
the app at a different SQLite database file and sends real account
credentials to the bridge; `_cmd_restart_app`/`_cmd_headless` spawn a real
OS subprocess and can force-exit the process via the ported
`_delayed_app_shutdown` helper (only ever scheduled fire-and-forget via
`asyncio.create_task`, never awaited here, same as every Telegram-alert
call throughout this migration series).

`_start_bridge_process` (the actual subprocess/Wine-teardown logic) is
taken as an explicit injected async callable -- it belongs to the separate,
not-yet-migrated background-loops cluster.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)


async def _delayed_app_shutdown(delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    if db_module.get_app_config("headless_mode_enabled") == "1":
        # No NiceGUI ASGI server running in headless mode to gracefully shut
        # down — nicegui.app.shutdown() would no-op here. The relaunch
        # subprocess was already spawned separately (with its own delay) by
        # whichever command triggered this, so ending this process is all
        # that's needed; it starts the next instance on its own.
        import os as _os
        _os._exit(0)
        return
    # Through os_utils rather than importing nicegui here: the backend must
    # stay runnable without a UI framework, and doing this in two places put
    # no-nicegui-in-the-backend over its baseline for an identical call.
    from backend.src.utils.os_utils import shutdown_ui
    shutdown_ui()


async def cmd_restart_bridge(
    args: list, bridge: Any,
    start_bridge_process: Callable[[], Awaitable[bool]],
) -> str:
    """Stop and restart the MT5 bridge process, then enable AutoTrading."""
    launched = await start_bridge_process()
    if not launched:
        return "Could not start bridge — check Settings > MT5 / Bridge > Start Bridge."

    # Poll for port 9000.  Wine/CrossOver startup takes 15-30 s on macOS;
    # native Windows bridge is up in 2-3 s.
    from backend.src.utils.os_utils import is_port_listening as _ipl
    bridge_up = False
    for _ in range(12):
        await asyncio.sleep(3)
        try:
            bridge_up = _ipl(9000)
        except Exception:
            bridge_up = False
        if bridge_up:
            break

    if not bridge_up:
        return (
            "Bridge process launched but port 9000 not bound after 36s.\n"
            "Check Settings > MT5 / Bridge for errors."
        )

    # Allow the bridge a moment to complete its initial MT5 connect
    await asyncio.sleep(3)

    health = await bridge.get_health()
    if not health.get("connected", False):
        return (
            "Bridge is up on port 9000 but MT5 is not connected yet.\n"
            "It will retry automatically every 30s — check /status shortly."
        )

    # Bridge is connected — check and ensure AutoTrading is active
    if health.get("trade_allowed"):
        return "Bridge restarted and connected. Algo Trading: active."

    at = await bridge.enable_autotrading()
    if at.get("enabled"):
        method = at.get("method", "")
        if method == "already_enabled":
            return "Bridge restarted and connected. Algo Trading: active."
        return "Bridge restarted and connected. Algo Trading: enabled automatically."

    at_err = at.get("error", "unknown")
    return (
        "Bridge restarted and connected to MT5.\n"
        "Algo Trading: DISABLED — re-enable the AutoTrading button in the MT5 terminal toolbar.\n"
        f"Auto-enable failed: {at_err}"
    )


async def cmd_restart_app(args: list, bot_offset: int) -> str:
    """Restart the FOREX Trader app process (5-second delay so reply can send)."""
    try:
        # Marker-based, not a parent count: this module moved during the refactor
        # and the fixed count silently resolved inside backend/ instead of the
        # checkout root. Same bug that made the restart button relaunch a run.py
        # that does not exist (2026-08-26).
        from backend.src.utils.os_utils import repo_root as _repo_root
        root = _repo_root()
        if sys.platform == "win32":
            venv_python = root / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = root / ".venv" / "bin" / "python3"
        python = str(venv_python) if venv_python.exists() else sys.executable
        from backend.src.config import USER_DATA_DIR
        from backend.src.utils.os_utils import delayed_relaunch_cmd, open_restart_log
        log_path = USER_DATA_DIR / "data" / "restart.log"
        # Persist the current offset NOW so the restarted process skips
        # this /restartapp update and doesn't trigger another restart.
        db_module.set_app_config("bot_update_offset", str(bot_offset))
        with open_restart_log(log_path) as _f:
            subprocess.Popen(
                delayed_relaunch_cmd(python, "run.py", delay_secs=6, extra_args=["--no-browser"]),
                cwd=str(root),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=_f,
                stderr=_f,
            )
        asyncio.create_task(_delayed_app_shutdown(5))
        return "Restarting app in 5 seconds — reconnect your browser shortly."
    except Exception as e:
        return f"Restart failed: {e}"


async def cmd_headless(
    args: list, restart_app: Callable[[list], Awaitable[str]],
) -> str:
    """Toggle headless mode (no web UI) and restart to apply it.

    The flag is only read once, at process start (run.py), before it
    decides whether to call ui.run() at all — NiceGUI owns the event
    loop for the whole process once started, so this can't be a live
    hot-toggle. Reuses the exact same restart mechanism as /restartapp,
    just with the flag flipped first, so it's one command end to end
    rather than "set a setting, then remember to also restart."
    """
    if not args or args[0].lower() not in ("on", "off"):
        current = "ON" if db_module.get_app_config("headless_mode_enabled") == "1" else "OFF"
        return f"Usage: /headless on | off\nCurrently: {current}"
    action = args[0].lower()
    db_module.set_app_config("headless_mode_enabled", "1" if action == "on" else "0")
    restart_result = await restart_app([])
    if action == "on":
        return (
            "Headless mode enabled. " + restart_result + "\n"
            "The web UI will not be available after this restart — "
            "send /headless off to bring it back."
        )
    return "Headless mode disabled — the web UI will be available again. " + restart_result


async def cmd_switch_live(args: list, bridge: Any) -> str:
    return await cmd_switch_env("live", bridge)


async def cmd_switch_demo(args: list, bridge: Any) -> str:
    return await cmd_switch_env("demo", bridge)


async def cmd_switch_env(new_env: str, bridge: Any) -> str:
    """Switch the active MT5 account environment (live/demo)."""
    import backend.src.config as _cfg
    env_label = "Live" if new_env == "live" else "Demo"
    try:
        creds = db_module.get_mt5_credentials()
        if new_env == "live":
            login    = int(creds.get("live_login") or 0)
            password = creds.get("live_password_enc") or ""
            server   = (creds.get("live_server") or "").strip()
        else:
            login    = int(creds.get("login") or 0)
            password = creds.get("password_enc") or ""
            server   = (creds.get("server") or "").strip()

        if not login or not password or not server:
            return (
                f"{env_label} credentials not configured.\n"
                "Go to Settings > MT5 / Bridge and enter your "
                f"{env_label} account login, password and server first."
            )

        from backend.src.config import DATA_DIR as _DATA_DIR
        db_module.init(str(_DATA_DIR / f"forex_trader_{new_env}.db"))
        _cfg.save_to_yaml({"account_env": new_env})
        db_module.sync_bridge_credentials_file(new_env)

        result = await bridge.send_credentials(login, password, server)
        if result.get("status") == "connected":
            health = await bridge.get_health()
            at_note = (
                "\nNote: Algo Trading may have reset in MT5 — re-enable the AutoTrading button."
                if health.get("trade_allowed") is False else ""
            )
            return f"Switched to {env_label} account (login {login}).{at_note}"
        else:
            err = result.get("error") or result.get("status") or "unknown"
            return (
                f"Config saved for {env_label} but MT5 bridge returned: {err}\n"
                "Try /restartbridge to reconnect."
            )
    except Exception as e:
        return f"Switch to {env_label} failed: {e}"
