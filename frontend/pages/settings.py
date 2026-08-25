"""Settings page — MT5 credentials, API keys, risk settings."""

import asyncio
import os
import subprocess
import sys
from typing import Callable

from nicegui import app, ui

from backend.src.controllers import settings_controller as settings_ctl
from backend.src.utils import os_utils as _pu
from backend.src.controllers import sync_controller as sync_ctl
import backend.src.config as cfg_module
from backend.src.services.positions import core_autostart as _autostart
from backend.src.db import database as db_module
from backend.src.services.cluster.sync import client as sync_client

# ── Prevent-sleep state (module-level so it survives page re-renders) ──────────
# On macOS uses `caffeinate -i -w <app-pid>`.
# On Windows uses SetThreadExecutionState via platform_utils.
_caffeinate_proc = None   # holds Popen (macOS) or _WindowsSleepGuard (Windows)

# Current Claude model catalogue — update when Anthropic releases new versions
# claude-fable-5, claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5-20251001
CLAUDE_MODELS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

# Legacy model IDs that may be stored in older config files → map to current ID.
# Prevents ValueError when ui.select receives a value not in CLAUDE_MODELS.
_CLAUDE_MODEL_ALIASES: dict[str, str] = {
    "claude-haiku-4-5":   "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5":  "claude-sonnet-4-6",
    "claude-opus-4-5":    "claude-opus-4-8",
}
_CLAUDE_DEFAULT = "claude-sonnet-4-6"


def _resolve_claude_model(stored: str) -> str:
    """Return a valid CLAUDE_MODELS entry for *stored*, falling back to the default."""
    if stored in CLAUDE_MODELS:
        return stored
    if stored in _CLAUDE_MODEL_ALIASES:
        return _CLAUDE_MODEL_ALIASES[stored]
    return _CLAUDE_DEFAULT


def render(get_engine: Callable, get_tg_reader: Callable):
    engine = get_engine()

    with ui.tabs().classes("bg-gray-800") as stabs:
        t_mt5    = ui.tab("MT5 / Bridge")
        t_ai     = ui.tab("AI")
        t_tg_bot = ui.tab("Telegram Alerts")
        t_email  = ui.tab("Email Reports")
        t_remote = ui.tab("Remote Node")
        t_diag   = ui.tab("Diagnostics")
        t_reg    = ui.tab("Registration")
        t_upd    = ui.tab("Update")
        t_expert = ui.tab("Expert Tunables")
        t_theme  = ui.tab("Theme")

    with ui.tab_panels(stabs, value=t_mt5).classes("bg-gray-900 p-4"):
        with ui.tab_panel(t_mt5):
            _render_mt5(engine)
        with ui.tab_panel(t_ai):
            _render_ai(engine)
        with ui.tab_panel(t_tg_bot):
            _render_tg_bot()
        with ui.tab_panel(t_email):
            _render_email()
        with ui.tab_panel(t_remote):
            from frontend.pages.remote_node import render as _render_remote_node
            _render_remote_node(get_engine)
        with ui.tab_panel(t_diag):
            _run_diag = _render_diagnostics(engine)
        with ui.tab_panel(t_reg):
            _render_registration()
        with ui.tab_panel(t_upd):
            from frontend.pages.update_panel import render as _render_update
            _render_update()
        with ui.tab_panel(t_expert):
            from frontend.pages.expert_tunables import render as _render_expert_tunables
            _render_expert_tunables()
        with ui.tab_panel(t_theme):
            _render_theme()

    _diag_refresh_timer = [None]

    def _on_settings_tab_change(e):
        if e.value == t_diag:
            asyncio.create_task(_run_diag())
            if _diag_refresh_timer[0] is None:
                _diag_refresh_timer[0] = ui.timer(15.0, _run_diag)
        elif _diag_refresh_timer[0] is not None:
            # Only bridge/EA/tick calls, no heavier work -- fine to keep
            # cheap, but no reason to poll it while the user isn't looking.
            _diag_refresh_timer[0].cancel()
            _diag_refresh_timer[0] = None

    stabs.on_value_change(_on_settings_tab_change)


def _render_cred_card(engine, creds: dict, env: str, status_lbl) -> tuple:
    """Render one set of MT5 credentials. Returns (login, password, server, term_path) inputs."""
    is_live   = env == "live"
    pfx       = "live_" if is_live else ""
    title     = "MT5 Credentials — LIVE" if is_live else "MT5 Credentials — DEMO"
    color     = "text-red-400" if is_live else "text-yellow-300"
    icon      = "⚡ LIVE" if is_live else "🔵 DEMO"

    with ui.row().classes("w-full items-center gap-1"):
        login = ui.number(
            "Login", value=int(creds.get(f"{pfx}login", 0) or 0), format="%.0f"
        ).classes("flex-1")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            f"Your Vantage Markets {'LIVE' if is_live else 'DEMO'} account number."
        )

    with ui.row().classes("w-full items-center gap-1"):
        password = ui.input("Password", password=True).classes("flex-1")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            f"Your {'live' if is_live else 'demo'} MT5 password — stored locally, never transmitted off-device."
        )
    password.placeholder = "(leave blank to keep existing)"

    with ui.row().classes("w-full items-center gap-1"):
        server = ui.input(
            "Server", value=creds.get(f"{pfx}server", "") or "",
            placeholder="VantageMarkets-Live 6" if is_live else "VantageMarkets-Demo",
        ).classes("flex-1")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            f"{'VantageMarkets-Live' if is_live else 'VantageMarkets-Demo'} — "
            "must match the account type exactly."
        )

    with ui.row().classes("w-full items-center gap-1"):
        term_path = ui.input(
            "Terminal path (optional)",
            value=creds.get(f"{pfx}terminal_path", "") or "",
        ).classes("flex-1")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Full path to terminal64.exe. Leave blank for auto-detection."
        )

    return login, password, server, term_path


def _render_mt5(engine):
    import time as _t

    cfg = cfg_module.load()
    # Always read credentials from the master (demo) DB — env-independent
    creds = settings_ctl.get_mt5_credentials()

    status_lbl = ui.label("").classes("text-sm text-gray-400 mb-2")

    # ── Demo + Live side by side — equal fixed width ──────────────────────────
    with ui.row().classes("w-full gap-4 flex-wrap items-start"):
        for env in ("demo", "live"):
            is_live = env == "live"
            title   = "MT5 Credentials — LIVE ⚡" if is_live else "MT5 Credentials — DEMO 🔵"
            color   = "text-red-400 font-bold" if is_live else "text-yellow-300"

            with ui.card().classes("bg-gray-800 p-5 rounded-lg").style("width:340px; min-width:300px"):
                ui.label(title).classes(f"text-base {color} mb-3")

                login_v, pw_v, srv_v, tp_v = _render_cred_card(engine, creds, env, status_lbl)

                test_lbl = ui.label("").classes("text-xs mt-2 min-h-4")

                async def save(
                    _env=env, _login=login_v, _pw=pw_v, _srv=srv_v, _tp=tp_v,
                ):
                    try:
                        pfx  = "live_" if _env == "live" else ""
                        _pwd = _pw.value.strip() or creds.get(f"{pfx}password_enc", "")
                        updates: dict = {
                            f"{pfx}login":         int(_login.value or 0),
                            f"{pfx}server":        _srv.value.strip(),
                            f"{pfx}terminal_path": _tp.value.strip() or None,
                        }
                        if _pw.value.strip():
                            updates[f"{pfx}password_enc"] = _pw.value.strip()
                        if not pfx:
                            updates["account_type"] = "demo"
                            updates["updated_at"]   = _t.time()

                        # Always save to master (demo) credential store
                        settings_ctl.save_mt5_credentials(updates)

                        # Keep bridge_credentials.json in sync if this env is active
                        current_env = cfg_module.get("account_env", "demo")
                        if _env == current_env:
                            settings_ctl.sync_bridge_credentials_file(current_env)

                        # Push to bridge if this env is currently active
                        if _env == current_env:
                            try:
                                result = await engine._bridge.send_credentials(
                                    int(_login.value or 0), _pwd, _srv.value.strip()
                                )
                                br = result.get("status") or result.get("error") or "ok"
                                status_lbl.text = f"{_env.title()} credentials saved — bridge: {br}"
                            except Exception:
                                status_lbl.text = f"{_env.title()} credentials saved (bridge offline)"
                        else:
                            status_lbl.text = f"{_env.title()} credentials saved."

                        ui.notify(f"{_env.title()} credentials saved", type="positive")
                    except Exception as e:
                        status_lbl.text = str(e)
                        ui.notify(str(e), type="negative")

                async def test_conn(
                    _env=env, _login=login_v, _pw=pw_v, _srv=srv_v, _lbl=test_lbl,
                ):
                    pfx   = "live_" if _env == "live" else ""
                    saved = settings_ctl.get_mt5_credentials()
                    login_val = int(_login.value or 0)
                    pwd_val   = _pw.value.strip() or saved.get(f"{pfx}password_enc", "")
                    srv_val   = _srv.value.strip() or saved.get(f"{pfx}server", "")
                    if not login_val or not pwd_val or not srv_val:
                        _lbl.text = "Enter login, password and server first"
                        _lbl.classes(replace="text-xs mt-2 text-yellow-400")
                        return
                    _lbl.text = "Connecting..."
                    _lbl.classes(replace="text-xs mt-2 text-gray-400")
                    try:
                        result = await engine._bridge.send_credentials(
                            login_val, pwd_val, srv_val
                        )
                        if result.get("error"):
                            _lbl.text = f"Failed: {result['error']}"
                            _lbl.classes(replace="text-xs mt-2 text-red-400")
                        else:
                            _lbl.text = f"Connected: {result.get('status', 'ok')}"
                            _lbl.classes(replace="text-xs mt-2 text-green-400")
                    except Exception as ex:
                        _lbl.text = f"Bridge error: {ex}"
                        _lbl.classes(replace="text-xs mt-2 text-red-400")

                with ui.row().classes("gap-2 mt-3"):
                    ui.button(
                        f"Save {'Live' if is_live else 'Demo'} Credentials",
                        on_click=save,
                    ).classes("bg-blue-700 text-white px-4 py-2 flex-1")
                    ui.button(
                        "Test Connection",
                        on_click=test_conn,
                    ).classes("bg-gray-600 text-white px-4 py-2")

                test_lbl

    status_lbl

    ui.separator().classes("my-4")

    rs = settings_ctl.get_risk_settings()
    with ui.card().classes("w-full max-w-xl bg-gray-800 border border-blue-600 p-4 rounded-lg"):
        with ui.row().classes("w-full items-center justify-between"):
            ea_bridge_sw = ui.switch(
                "EA Bridge (experimental)",
                value=bool(rs.get("ea_bridge_enabled", 0)),
            ).classes("text-blue-300 font-bold")
            ui.icon("bolt", size="sm").classes("text-blue-400")
        with ui.expansion(
            "What does the EA Bridge do?", icon="info_outline"
        ).classes("w-full text-sm"):
            ui.markdown(
                "When **ON** and a companion MQL5 EA is attached to the XAUUSD chart "
                "on this node's MT5 terminal, new trades for portable strategies "
                "(Scale Out, BE Runner, Trail Stop, Protected Scale, Conservative, "
                "Scalp Runner, Conservative Trial, Signal Climber, Reversal Runner, "
                "Adaptive Runner, ORB Fixed, Trend Ratchet) are placed "
                "and managed by the EA directly inside MT5's own tick loop, instead "
                "of this app's polling cycle. DPM always stays app-managed — its "
                "parameters are continuously recalculated from live calibration data "
                "this app holds, which has no MT5-native equivalent.\n\n"
                "If the EA disconnects or stops responding, any trade it was managing "
                "is automatically reclaimed by the app — never left unmanaged.\n\n"
                "When **OFF**, every trade is managed by the app exactly as before."
            ).classes("text-gray-300")

        def _save_ea_bridge():
            settings_ctl.update_risk_settings({
                "ea_bridge_enabled": int(bool(ea_bridge_sw.value)),
            })
            ui.notify("EA Bridge setting saved", type="positive")

        ui.button("Save", icon="save", on_click=_save_ea_bridge).classes(
            "bg-blue-700 text-white text-xs px-3 py-1 mt-2"
        )

        ui.separator().classes("my-3")
        _render_ea_update_button()

    ui.separator().classes("my-4")
    _render_bridge_control(engine)


def _render_ea_update_button():
    """Compile mql5/ForexTraderBridge.mq5 and hot-reload it into this node's
    live MT5 terminal, with zero manual steps — see project memory
    'MT5 Terminal Management' for the hand-validated recipe this automates.

    Windows-only (MetaEditor headless compile + Scheduled Task terminal
    relaunch have no equivalent on the Mac's Wine-hosted MT5 install, which
    needs its own separate setup — not attempted here).
    """
    status_lbl = ui.label("").classes("text-xs text-gray-400")

    # This Python process exits mid-sequence (see below), so the only way to
    # show what happened is reading back the detached script's own log on
    # the NEXT page load — there's no live handler left to report through.
    try:
        from backend.src.config import USER_DATA_DIR
        _result_log = USER_DATA_DIR / "data" / "ea_update_result.log"
        if _result_log.exists():
            _lines = _result_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if _lines:
                ui.label(f"Last update: {_lines[-1]}").classes("text-xs text-gray-500")
    except Exception:
        pass

    async def _update_and_reload_ea():
        if sys.platform != "win32":
            ui.notify(
                "EA update/reload is only automated on the Windows VPS node. "
                "This node's terminal needs its own manual setup.",
                type="warning",
            )
            return

        from pathlib import Path
        import time as _time

        root = Path(__file__).parent.parent.parent.parent
        repo_mq5 = root / "mql5" / "ForexTraderBridge.mq5"
        if not repo_mq5.exists():
            ui.notify(f"EA source not found at {repo_mq5}", type="negative")
            return

        terminal_dir = Path(r"C:\Program Files\Vantage Markets MT5 Terminal")
        metaeditor = terminal_dir / "MetaEditor64.exe"
        # This VPS's one MT5 install's MetaQuotes data-folder id — fixed per
        # install, does not change across restarts/updates.
        terminal_id = "725B72F25E46C780EF59F57016D58156"
        data_dir = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal" / terminal_id
        remote_mq5 = data_dir / "MQL5" / "Experts" / "ForexTraderBridge.mq5"
        compile_log = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "ea_compile.log"

        if not metaeditor.exists():
            ui.notify(f"MetaEditor64.exe not found at {metaeditor}", type="negative")
            return

        status_lbl.set_text("Compiling...")
        ui.notify("Compiling EA...", type="info")
        try:
            remote_mq5.parent.mkdir(parents=True, exist_ok=True)
            remote_mq5.write_bytes(repo_mq5.read_bytes())
            compile_log.unlink(missing_ok=True)
            proc = await asyncio.create_subprocess_exec(
                str(metaeditor), f"/compile:{remote_mq5}", f"/log:{compile_log}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception as e:
            status_lbl.set_text(f"Compile step failed: {e}")
            ui.notify(f"EA compile step failed: {e}", type="negative")
            return

        await asyncio.sleep(1)  # MetaEditor writes the log a beat after the process exits
        log_text = ""
        for enc in ("utf-16", "utf-8", "latin-1"):
            try:
                log_text = compile_log.read_text(encoding=enc, errors="replace")
                break
            except Exception:
                continue
        if "0 errors" not in log_text:
            status_lbl.set_text("Compile FAILED — live system untouched. See ea_compile.log on the VPS.")
            ui.notify("EA compile failed — aborting. Live terminal/app untouched.", type="negative")
            return

        status_lbl.set_text("Compile OK (0 errors). Restarting terminal + app to load it...")
        ui.notify(
            "Compile OK. Restarting the MT5 terminal and this app to load the new EA — "
            "browser reconnects automatically in ~40s.",
            type="info",
        )

        # From here on this Python process is about to be killed as part of
        # the sequence — everything past this point runs in a detached script
        # that survives that, mirroring the existing Restart button's own
        # self-relaunch pattern (ui/app.py's _do_restart) but with the MT5
        # terminal restart sandwiched in between stop and relaunch.
        from backend.src.config import USER_DATA_DIR as _ea_user_data_dir
        script = _EA_RESTART_PS1_TEMPLATE.format(
            terminal_id=terminal_id, data_folder=_ea_user_data_dir.name,
        )
        script_path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "ea_update_restart.ps1"
        script_path.write_text(script, encoding="utf-8")

        from backend.src.utils.os_utils import open_restart_log
        from backend.src.config import USER_DATA_DIR
        log_path = USER_DATA_DIR / "data" / "ea_update_restart.log"
        with open_restart_log(log_path) as _restart_log:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                cwd=str(root),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=_restart_log,
                stderr=_restart_log,
            )
        await asyncio.sleep(1)
        app.shutdown()

    with ui.row().classes("items-center gap-2"):
        ui.button(
            "Update & Reload EA", icon="sync",
            on_click=lambda: asyncio.create_task(_update_and_reload_ea()),
        ).classes("bg-purple-700 text-white text-xs px-3 py-1")
        ui.icon("info_outline", size="xs").classes("text-gray-500").tooltip(
            "Compiles mql5/ForexTraderBridge.mq5 and hot-reloads it into this "
            "node's live MT5 terminal. Stops trading briefly (~30-40s) while "
            "the terminal restarts. Aborts with zero live impact if the "
            "compile itself fails — only proceeds to touch anything live "
            "once a clean 0-error build exists."
        )
    status_lbl


# Detached PowerShell sequence launched by _render_ea_update_button's handler
# right before this Python process exits — see that function's own comment
# for why this can't just run inline. Hand-validated manually (2026-07-17)
# before being wrapped into this template; see project memory 'MT5 Terminal
# Management' for the underlying recipe and its hard-won gotchas (never a
# bare force-kill if it can be avoided, and Python must be fully stopped
# before the terminal relaunch or its own reconnect loop races the
# config-carrying launch and silently wins, leaving the EA never attached).
_EA_RESTART_PS1_TEMPLATE = r'''
$logPath = "$env:APPDATA\{data_folder}\data\ea_update_result.log"
function Log($msg) {{ "$(Get-Date -Format o)  $msg" | Out-File -FilePath $logPath -Append -Encoding utf8 }}
Log "EA update sequence starting"

Disable-ScheduledTask -TaskName "FOREX Trader External Watchdog" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "FOREX Trader Launcher" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Log "Python stopped"

$procs = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($procs) {{
    foreach ($p in $procs) {{ $p.CloseMainWindow() | Out-Null }}
    Start-Sleep -Seconds 5
    if (Get-Process terminal64 -ErrorAction SilentlyContinue) {{
        Stop-Process -Name terminal64 -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }}
}}
Log "terminal64 closed"

$iniPath = "$env:USERPROFILE\mt5_startup.ini"
@"
[StartUp]
Expert=ForexTraderBridge
Symbol=XAUUSD
Period=H1
"@ | Out-File -FilePath $iniPath -Encoding ASCII

$exePath = "C:\Program Files\Vantage Markets MT5 Terminal\terminal64.exe"
$taskName = "TempEARestart"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute $exePath -Argument "/config:`"$iniPath`""
$principal = New-ScheduledTaskPrincipal -UserId "Administrator" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 10
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Log "terminal64 relaunched via scheduled task"

$termLogDir = "$env:APPDATA\MetaQuotes\Terminal\{terminal_id}\Logs"
$today = Get-Date -Format "yyyyMMdd"
$termLog = Join-Path $termLogDir "$today.log"
$loaded = $false
for ($i = 0; $i -lt 15; $i++) {{
    if (Test-Path $termLog) {{
        $content = Get-Content $termLog -Tail 50 -Encoding Unicode -ErrorAction SilentlyContinue
        if ($content -match "expert ForexTraderBridge.*loaded successfully") {{ $loaded = $true; break }}
    }}
    Start-Sleep -Seconds 2
}}
if ($loaded) {{ Log "EA loaded successfully - VERIFIED" }} else {{ Log "WARNING: EA load NOT verified after 30s - check terminal manually" }}

Start-ScheduledTask -TaskName "FOREX Trader Launcher"
Start-Sleep -Seconds 5
Enable-ScheduledTask -TaskName "FOREX Trader External Watchdog" -ErrorAction SilentlyContinue
Log "Python restarted, watchdog re-enabled. Sequence complete."
'''


_RISK_SUBCARD_CLASSES = "flex-1 min-w-72 bg-gray-800 p-3 rounded-lg"


def render_risk_card(card_classes: str = "w-full"):
    """Risk settings, split (2026-08-01) into five side-by-side sub-cards
    across two rows for the same tidy layout the rest of the Trading page
    uses (Strategy Parameters/Channel Strategy already sit side by side the
    same way) rather than one long scrolling card:

    Row 1: Risk Settings | Circuit Breaker | Toxic-Hour Blocklist
    Row 2: Internal Engine Exposure | Dynamic Position Management

    `card_classes` now sizes the OUTER wrapper (was previously the single
    inner ui.card()'s classes, back when this was one card) -- importable by
    other pages exactly as before, just laid out differently inside.

    Risk per trade (%) / Max risk per trade (%) and Fixed Lot Size all moved
    to Trading > Global Parameters (2026-07-24), so the two no longer need
    to be greyed in/out against each other from here.
    """
    rs = settings_ctl.get_risk_settings()

    with ui.column().classes(f"{card_classes} gap-4"):
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
            _render_risk_settings_subcard(rs)
            _render_circuit_breaker_subcard(rs)
            _render_toxic_hour_subcard(rs)

        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
            _render_internal_exposure_subcard(rs)
            _render_dpm_subcard(rs)


def _render_risk_settings_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("shield", size="sm").classes("text-yellow-400")
            ui.label("Risk Settings").classes("text-base font-bold text-yellow-300")

        # ── Risk Governor master toggle (Tier 1 safety layer) ─────────────────
        with ui.card().classes("w-full bg-gray-800 border border-yellow-600 mb-4 p-3"):
            with ui.row().classes("w-full items-center justify-between"):
                risk_gov = ui.switch(
                    "Risk Governor",
                    value=bool(rs.get("risk_governor_enabled", 0)),
                ).classes("text-yellow-300 font-bold")
                ui.icon("shield", size="sm").classes("text-yellow-400")
            with ui.expansion(
                "What does the Risk Governor do?", icon="info_outline"
            ).classes("w-full text-sm"):
                ui.markdown(
                    "When **ON**, a deterministic safety layer sits underneath *every* "
                    "strategy and applies these rules before and after each trade. It "
                    "never changes which strategy runs — only the size and frequency of "
                    "trades, so no single trade or bad day can blow up the account:\n\n"
                    "- **Risk-based position sizing** — lot size is calculated from "
                    "*Risk per trade (%)* and the actual stop distance instead of a fixed "
                    "lot. A wider stop is automatically sized smaller.\n"
                    "- **Hard per-trade ceiling** — no single trade may risk more than "
                    "*Max risk per trade (%)* of balance; trades that cannot fit are skipped.\n"
                    "- **Stop-width cap** — rejects any scalp whose stop is wider than "
                    "1.5x the current ATR.\n"
                    "- **Reward:risk floor** — blocks badly-inverted setups where the "
                    "first target is closer than a third of the stop distance (skipped "
                    "for strategies that set their own levels after fill, e.g. Conservative).\n"
                    "- **Directional cap** — at most 2 unprotected same-direction trades "
                    "open at once (no stacking into one side).\n"
                    "- **Daily-loss circuit breaker** — once today's realised losses reach "
                    "*Max daily loss (%)*, auto-trading pauses until the next trading day.\n\n"
                    "When **OFF**, none of the above runs and strategies behave exactly as "
                    "before. Toggle it to compare performance with and without the governor."
                ).classes("text-gray-300")

        ui.label(
            "Risk per trade % / Max risk per trade % moved to Trading > Global Parameters."
        ).classes("text-xs text-gray-500 italic mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            max_dd = ui.number(
                "Max daily loss (%)", value=float(rs.get("max_daily_loss_pct", 3.0)),
                min=0.1, max=100, step=0.5, format="%.1f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "If today's total losses exceed this percentage of balance, "
                "no new trades will be opened until the next trading day."
            )

        with ui.row().classes("w-full items-center gap-1"):
            max_tot_dd = ui.number(
                "Max total drawdown (%)", value=float(rs.get("max_total_drawdown_pct", 8.0)),
                min=0.1, max=100, step=1.0, format="%.1f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "If the account drops this percentage below its all-time high, "
                "all auto-execution is paused."
            )

        # ── Give-back guard ──────────────────────────────────────────────
        # Measures from the day's PEAK, not its opening balance, which is the
        # only way to express "protect the profit I had". Both limits above
        # measure from the open and so cannot see a day that goes +$349 and
        # closes -$88 -- which is what 2026-08-17 did.
        ui.separator().classes("my-3")
        with ui.row().classes("w-full items-center gap-1"):
            giveback_sw = ui.switch(
                "Stop for the day after giving back today's profit",
                value=bool(rs.get("giveback_guard_enabled", 0)),
            ).classes("text-sm text-blue-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Max daily loss measures from the day's OPENING balance, so a "
                "day that rises and then bleeds back never breaches it. This "
                "measures from the day's PEAK instead: once the day is up by "
                "the arming amount, handing back more than the set share of "
                "that peak stops new trades until the next broker day.\n\n"
                "Open trades are unaffected — this blocks new entries only, "
                "and Resume (or /resume) lifts it early.\n\n"
                "Unlike the limits above, this does NOT require the Risk "
                "Governor to be on."
            )
        with ui.row().classes("w-full items-center gap-2"):
            giveback_arm = ui.number(
                "Arm above profit ($)", value=float(rs.get("giveback_arm_usd", 50.0)),
                min=0, max=100000, step=10, format="%.0f",
            ).classes("flex-1").tooltip(
                "The guard only arms once the day's realised profit has reached "
                "this. Below it, ordinary churn around break-even can never lock "
                "the day out."
            )
            giveback_pct = ui.number(
                "Give-back limit (%)", value=float(rs.get("giveback_pct", 40.0)),
                min=1, max=99, step=5, format="%.0f",
            ).classes("flex-1").tooltip(
                "How much of the day's peak profit may be handed back before "
                "stopping. 40 = stop once 40% of the peak is gone."
            )
        giveback_arm.bind_visibility_from(giveback_sw, "value")
        giveback_pct.bind_visibility_from(giveback_sw, "value")

        with ui.row().classes("w-full items-center gap-1"):
            max_trades = ui.number(
                "Max open trades", value=int(rs.get("max_open_trades", 1)),
                min=1, max=10, step=1, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Maximum number of simultaneously open positions. "
                "New signals are ignored if this limit is reached."
            )

        with ui.row().classes("w-full items-center gap-1"):
            max_lot = ui.number(
                "Max lot size", value=float(rs.get("max_lot_size", 0.10)),
                min=0.01, max=100, step=0.01, format="%.2f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Absolute maximum lot size regardless of risk calculation. "
                "Prevents oversized trades in volatile conditions."
            )

        ui.separator().classes("my-3")
        excl_high = ui.checkbox(
            "Exclude High-Risk",
            value=bool(rs.get("exclude_high_risk", 0)),
        ).classes("text-sm text-gray-300")
        excl_high.tooltip(
            "When checked, any Telegram signal containing 'High Risk' is silently ignored and not traded."
        )

        def save_risk():
            try:
                db_module.update_risk_settings({
                    "risk_governor_enabled":          int(bool(risk_gov.value)),
                    "max_daily_loss_pct":             float(max_dd.value      or 0),
                    "max_total_drawdown_pct":         float(max_tot_dd.value  or 0),
                    "max_open_trades":                int(max_trades.value    or 1),
                    "max_lot_size":                   float(max_lot.value     or 0),
                    "exclude_high_risk":              int(bool(excl_high.value)),
                    "giveback_guard_enabled":         int(bool(giveback_sw.value)),
                    "giveback_arm_usd":               float(giveback_arm.value or 0),
                    "giveback_pct":                   float(giveback_pct.value or 0),
                })
                ui.notify("Risk settings saved", type="positive")
            except (TypeError, ValueError) as _save_err:
                ui.notify(f"Invalid value — {_save_err}", type="negative")

        ui.button("Save Risk Settings", on_click=save_risk).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )


def _render_circuit_breaker_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon("block", size="sm").classes("text-red-400")
            ui.label("Circuit Breaker").classes("text-sm font-semibold text-red-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Blocks live MT5 trade execution after N consecutive losses. "
                "Signal engines and Telegram parsing continue — trades are queued "
                "and will activate once the cooldown expires."
            )

        cb_enabled = ui.checkbox(
            "Enable Circuit Breaker",
            value=bool(rs.get("circuit_breaker_enabled", 0)),
        ).classes("text-sm")

        with ui.row().classes("w-full items-center gap-2"):
            cb_losses = ui.number(
                "Consecutive losses to trigger",
                value=int(rs.get("circuit_breaker_losses", 3) or 3),
                min=1, max=20, step=1, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Number of consecutive losing live trades before the circuit breaker activates."
            )

        with ui.row().classes("w-full items-center gap-2"):
            cb_cooldown = ui.number(
                "Cooldown period (minutes)",
                value=int(rs.get("circuit_breaker_cooldown_mins", 60) or 60),
                min=1, max=1440, step=5, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "How long (in minutes) live trading is blocked after the circuit breaker triggers."
            )

        def _cb_reset():
            settings_ctl.reset_circuit_breaker()
            ui.notify("Circuit breaker reset — live trading unblocked.", type="positive")

        with ui.row().classes("items-center gap-2 mt-1"):
            ui.button("Reset Circuit Breaker Now", on_click=_cb_reset).props("dense outline").classes(
                "text-xs text-red-300 border-red-600"
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Immediately clears an active circuit breaker and resets the loss counter."
            )

        def save_cb():
            try:
                db_module.update_risk_settings({
                    "circuit_breaker_enabled":        int(bool(cb_enabled.value)),
                    "circuit_breaker_losses":         int(cb_losses.value     or 3),
                    "circuit_breaker_cooldown_mins":  int(cb_cooldown.value   or 60),
                })
                ui.notify("Circuit breaker settings saved", type="positive")
            except (TypeError, ValueError) as _save_err:
                ui.notify(f"Invalid value — {_save_err}", type="negative")

        ui.button("Save Circuit Breaker", on_click=save_cb).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )


def _render_toxic_hour_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        hour_block_val = bool(rs.get("hour_blocklist_enabled", 0))

        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("block").classes("text-red-400 text-base")
            ui.label("Toxic-Hour Blocklist").classes(
                "text-sm font-semibold text-red-300"
            )
            hour_block_badge = ui.badge(
                "BLOCKLIST ON" if hour_block_val else "BLOCKLIST OFF",
                color="red" if hour_block_val else "grey",
            )

        hour_block_chk = ui.checkbox(
            "Skip live execution during measured toxic hours",
            value=hour_block_val,
        ).classes("text-sm text-gray-200")

        def _hour_block_toggle(e):
            settings_ctl.update_risk_settings({"hour_blocklist_enabled": 1 if e.value else 0})
            hour_block_badge.props(f"color={'red' if e.value else 'grey'}")
            hour_block_badge.text = "BLOCKLIST ON" if e.value else "BLOCKLIST OFF"
            ui.notify(
                "Toxic-hour blocklist enabled" if e.value
                else "Toxic-hour blocklist disabled — all hours trade normally",
                type="positive" if e.value else "info",
            )

        hour_block_chk.on_value_change(_hour_block_toggle)

        ui.label(
            "Shared by the Bounce and Breakout generators. When OFF (default), "
            "every hour trades normally. When ON, the specific UTC hours each "
            "engine has measured as historically loss-making still generate and "
            "log signals as normal — so the ML models keep learning from real "
            "market data in those hours — they're just never placed as a real "
            "MT5 order."
        ).classes("text-xs text-gray-400 mt-1 leading-relaxed")


def _render_internal_exposure_subcard(rs: dict) -> None:
    from backend.src.services.positions import core_internal_exposure_guard as _ieg

    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("compare_arrows").classes("text-blue-400 text-base")
            ui.label("Internal Engine Exposure").classes(
                "text-sm font-semibold text-blue-300"
            )
            ui.icon("info_outline", size="xs").classes(
                "text-blue-400 cursor-help"
            ).tooltip(
                "Applies ONLY to the internal signal generators "
                "(Reversal, Breakout, Bounce). Telegram-channel trades "
                "are never affected by this setting."
            )
        ui.label(
            "The internal engines have no hedge guard of their own — each engine only "
            "blocks a duplicate in the SAME direction at the same level, and the "
            "cross-engine check ignores an engine's own signals. So a BUY and a SELL "
            "can sit open together. This controls whether that's allowed."
        ).classes("text-xs text-gray-500 mb-2")

        hedge_mode_sel = ui.select(
            {
                _ieg.MODE_OFF:          "Off — no restriction (default)",
                _ieg.MODE_SELF_HEDGE:   "Self-Hedge Guard — block opposing positions",
                _ieg.MODE_NET_EXPOSURE: "Net Exposure Cap — limit net directional lots",
            },
            value=(rs.get("internal_hedge_mode") or _ieg.MODE_OFF),
            label="Mode",
        ).classes("w-full").props("dense outlined")

        net_cap_num = ui.number(
            "Net exposure cap (lots)",
            value=float(rs.get("internal_net_exposure_max_lots", 0.30) or 0.30),
            min=0.0, step=0.01, format="%.2f",
        ).classes("w-full mt-1").props("dense outlined").tooltip(
            "Only used by Net Exposure Cap mode. Net = total BUY lots minus "
            "total SELL lots across all open internal-engine trades. "
            "0 = no cap (mode effectively disabled)."
        )

        with ui.column().classes("gap-0 mt-2"):
            ui.label("Off — no restriction (default)").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Long-standing behaviour, nothing is blocked. Opposing positions are "
                "allowed to open freely. Worth knowing before changing this: on 86 "
                "closed Reversal Engine trades (21–27 Jul), overlapping opposing pairs "
                "were 19% of trades but produced ~80% of total profit — 7 of 8 pairs "
                "had both legs win, and only one pair ever cancelled out (−$15.21). "
                "For a mean-reversion engine, buying support while selling resistance "
                "in a range is the strategy working, not a fault."
            ).classes("text-xs text-gray-500 mb-2")

            ui.label("Self-Hedge Guard").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Blocks a new internal-engine trade whenever ANY opposing-direction "
                "internal trade is already open. Strictest option: one direction at a "
                "time across all three engines combined. Best suited to trending "
                "conditions, where holding both sides tends to mean one leg is simply "
                "wrong. Costs you the range-play behaviour described above."
            ).classes("text-xs text-gray-500 mb-2")

            ui.label("Net Exposure Cap").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Allows opposing positions but caps how far the book can lean one way. "
                "A hedge is always permitted because it REDUCES net exposure; what "
                "gets blocked is stacking further in whichever direction already "
                "dominates. Example at a 0.30 cap: net +0.20 long, a new 0.10 BUY "
                "takes it to +0.30 and is allowed; a further 0.10 BUY would reach "
                "+0.40 and is blocked — but a 0.10 SELL is allowed at any time, since "
                "it brings net back toward flat. The middle-ground option: keeps the "
                "range play, limits one-way pile-ups. A cap of 0 disables the check."
            ).classes("text-xs text-gray-500 mb-1")

        ui.label(
            "Blocked trades are skipped for live execution only — the signal is still "
            "generated, tracked, and used for learning, exactly like the other "
            "execution gates."
        ).classes("text-xs text-gray-600 italic mb-1")

        def save_strategy():
            try:
                _hm = hedge_mode_sel.value
                if isinstance(_hm, dict):
                    _hm = _hm.get("value")
                settings_ctl.update_risk_settings({
                    "internal_hedge_mode":            _hm or _ieg.MODE_OFF,
                    "internal_net_exposure_max_lots": float(net_cap_num.value or 0.30),
                })
                ui.notify("Settings saved", type="positive")
            except Exception as ex:
                ui.notify(str(ex), type="negative")

        ui.button("Save Settings", on_click=save_strategy).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
        )


def _render_dpm_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("psychology").classes("text-blue-400 text-base")
            ui.label("Dynamic Position Management").classes(
                "text-sm font-semibold text-blue-300"
            )
            dpm_enabled_val = bool(rs.get("dpm_enabled", 0))
            dpm_badge = ui.badge(
                "DPM ON" if dpm_enabled_val else "DPM OFF",
                color="blue" if dpm_enabled_val else "grey",
            )

        dpm_chk = ui.checkbox(
            "Hand off to adaptive management",
            value=dpm_enabled_val,
        ).classes("text-sm text-gray-200")

        def _dpm_toggle(e):
            db_module.update_risk_settings({"dpm_enabled": 1 if e.value else 0})
            dpm_badge.props(f"color={'blue' if e.value else 'grey'}")
            dpm_badge.text = "DPM ON" if e.value else "DPM OFF"
            ui.notify(
                "DPM enabled — strategy control handed off" if e.value
                else "DPM disabled — strategy selection restored",
                type="positive" if e.value else "info",
            )

        dpm_chk.on_value_change(_dpm_toggle)

        # ── DPM Profit Take ─────────────────────────────────────────────────
        with ui.row().classes("items-end gap-2 mt-2 w-full"):
            with ui.column().classes("flex-1 gap-0"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Profit Take ($)").classes(
                        "text-xs text-gray-400 font-medium"
                    )
                    ui.icon("info_outline", size="xs").classes(
                        "text-blue-400 cursor-help"
                    ).tooltip(
                        "Close the remaining position when cumulative profit — "
                        "partial closes already taken plus unrealised P&L on "
                        "remaining lots — reaches this amount.\n"
                        "Example: set $150 and DPM will keep managing the trade "
                        "through its normal TP levels until the running total "
                        "hits $150, then close everything.\n"
                        "0 = DPM decides entirely (no dollar cap)."
                    )
                dpm_profit_inp = ui.number(
                    value=float(rs.get("profit_close_usd", 0.0) or 0.0),
                    min=0.0, step=5.0, format="%.2f",
                    placeholder="0 = DPM decides",
                ).classes("w-full")

            def _save_dpm_profit():
                try:
                    val = max(0.0, float(dpm_profit_inp.value or 0))
                    db_module.update_risk_settings({"profit_close_usd": val})
                    if val > 0:
                        ui.notify(
                            f"Profit take set to ${val:.2f} — DPM will close when "
                            f"cumulative profit reaches this amount",
                            type="positive",
                        )
                    else:
                        ui.notify(
                            "Profit take cleared — DPM manages profit levels entirely",
                            type="info",
                        )
                except Exception as ex:
                    ui.notify(str(ex), type="negative")

            ui.button("Set", on_click=_save_dpm_profit).classes(
                "bg-blue-700 text-white px-3 text-xs"
            ).style("height:30px; min-width:44px;")

        ui.label(
            "Automatically adjusts trail distance, breakeven timing and "
            "partial close size using ATR, session and momentum. "
            "Set a Profit Take amount above to cap the cumulative target — "
            "otherwise DPM decides entirely."
        ).classes("text-xs text-gray-400 mt-2 leading-relaxed")


async def _push_ai_config_to_vps(updates: dict) -> None:
    """Mirror an AI provider/model/key change to the paired VPS, if connected
    — best-effort, matching push_trade_closed()'s fire-and-forget pattern.
    Settings > AI has always been per-node (a separate config.yaml on each
    side, unlike the risk-settings sync); this is what makes "the provider I
    picked here" actually apply on both nodes instead of only the one whose
    UI you happened to save it on."""
    try:
        await sync_ctl.push_ai_config(updates)
    except Exception:
        pass


def _render_ai(engine):
    """AI provider settings: Claude / DeepSeek sub-tabs plus which one is active
    across the app (trade commentary, market analysis, channel strategy AI,
    breakout/test-signal review gates, the strategy builder)."""
    cfg = cfg_module.load()

    with ui.card().classes("w-full max-w-xl bg-gray-800 border border-purple-600 p-4 rounded-lg mb-4"):
        ui.label("AI Provider").classes("text-base font-bold text-yellow-300 mb-2")
        ui.label(
            "Whichever provider is selected here is used everywhere the app calls an "
            "AI model — trade commentary, market analysis, channel strategy selection, "
            "signal quality review, and the strategy builder."
        ).classes("text-xs text-gray-400 mb-3")
        provider_radio = ui.radio(
            {"claude": "Claude", "deepseek": "DeepSeek"},
            value=cfg.get("ai_provider", "claude") if cfg.get("ai_provider") in ("claude", "deepseek") else "claude",
        ).props("inline")

        async def _save_provider():
            cfg_module.save_to_yaml({"ai_provider": provider_radio.value})
            engine._cfg["ai_provider"] = provider_radio.value
            await _push_ai_config_to_vps({"ai_provider": provider_radio.value})
            ui.notify(f"AI provider set to {provider_radio.value.title()}", type="positive")

        ui.button("Save", icon="save", on_click=_save_provider).classes(
            "bg-blue-700 text-white text-xs px-3 py-1 mt-1"
        )

    with ui.tabs().classes("bg-gray-800") as ai_tabs:
        t_claude   = ui.tab("Claude")
        t_deepseek = ui.tab("DeepSeek")

    with ui.tab_panels(ai_tabs, value=t_claude).classes("bg-gray-900 p-0"):
        with ui.tab_panel(t_claude):
            _render_claude_card(engine)
        with ui.tab_panel(t_deepseek):
            _render_deepseek_card(engine)


def _render_claude_card(engine):
    from backend.src.services.ai import provider as ai_provider
    cfg = cfg_module.load()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Claude Integration").classes("text-base font-bold text-yellow-300 mb-3")

        with ui.row().classes("w-full items-center gap-1"):
            api_key_in = ui.input(
                "Anthropic API Key", value=cfg.get("anthropic_api_key", ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Required for Claude AI trade commentary. Get yours at console.anthropic.com."
            )

        _model_options = cfg.get("claude_models_cache") or CLAUDE_MODELS
        with ui.row().classes("w-full items-center gap-1"):
            claude_model_in = ui.select(
                _model_options,
                value=_resolve_claude_model(cfg.get("claude_model", _CLAUDE_DEFAULT))
                      if cfg.get("claude_model", _CLAUDE_DEFAULT) in _model_options
                      else (_model_options[0] if _model_options else _CLAUDE_DEFAULT),
                label="Claude Model",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Model used for trade commentary and signal analysis. "
                "The list refreshes daily from Anthropic's own model catalogue — "
                "use Refresh below to check right now."
            )

        refresh_lbl = ui.label(
            f"Model list last refreshed: "
            f"{_fmt_last_refresh(cfg.get('ai_models_last_refreshed', 0))}"
        ).classes("text-xs text-gray-500")

        async def _refresh_models():
            refresh_lbl.text = "Refreshing model list..."
            models = await ai_provider.fetch_available_models("claude", api_key_in.value)
            claude_model_in.set_options(models)
            now = __import__("time").time()
            cfg_module.save_to_yaml({"claude_models_cache": models, "ai_models_last_refreshed": now})
            engine._cfg["claude_models_cache"]      = models
            engine._cfg["ai_models_last_refreshed"] = now
            refresh_lbl.text = f"Model list last refreshed: {_fmt_last_refresh(now)}"

        test_result = ui.label("").classes("text-sm mt-1")

        async def test_anthropic():
            test_result.text = "Testing..."
            test_result.classes(replace="text-sm mt-1 text-gray-400")
            try:
                # Tests the key/model currently typed in the form — not yet
                # saved — by routing through the same ai_provider.complete()
                # abstraction Run Analysis/Strategy Builder use, via a
                # transient cfg dict, rather than a separate hardcoded
                # anthropic.Anthropic() call that could silently drift out of
                # sync with how the app actually calls Claude.
                _test_cfg = {
                    "ai_provider":       "claude",
                    "anthropic_api_key": api_key_in.value,
                    "claude_model":      claude_model_in.value,
                }
                await ai_provider.complete(_test_cfg, "", "ping", max_tokens=5, timeout=15)
                test_result.text = f"Connected — model: {claude_model_in.value}"
                test_result.classes(replace="text-sm mt-1 text-green-400")
            except Exception as e:
                test_result.text = f"Failed: {e}"
                test_result.classes(replace="text-sm mt-1 text-red-400")

        async def save_config():
            cfg_module.save_to_yaml({
                "anthropic_api_key": api_key_in.value,
                "claude_model":      claude_model_in.value,
            })
            engine._cfg["anthropic_api_key"] = api_key_in.value
            engine._cfg["claude_model"]      = claude_model_in.value
            # anthropic_api_key deliberately not pushed to the VPS — it may have
            # its own separately-provisioned Claude key/account; only the model
            # choice is synced, matching _AI_CONFIG_SYNC_KEYS in sync/server.py.
            await _push_ai_config_to_vps({"claude_model": claude_model_in.value})
            ui.notify("Config saved", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save Config", on_click=save_config).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Test Connection", on_click=test_anthropic).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
            ui.button("Refresh Models", icon="refresh", on_click=_refresh_models).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
        test_result


def _render_deepseek_card(engine):
    from backend.src.services.ai import provider as ai_provider
    cfg = cfg_module.load()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("DeepSeek Integration").classes("text-base font-bold text-yellow-300 mb-3")

        with ui.row().classes("w-full items-center gap-1"):
            api_key_in = ui.input(
                "DeepSeek API Key", value=cfg.get("deepseek_api_key", ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Required for DeepSeek trade commentary. Get yours at platform.deepseek.com."
            )

        _model_options = cfg.get("deepseek_models_cache") or ai_provider.FALLBACK_DEEPSEEK_MODELS
        _stored_model = cfg.get("deepseek_model", "")
        with ui.row().classes("w-full items-center gap-1"):
            deepseek_model_in = ui.select(
                _model_options,
                value=_stored_model if _stored_model in _model_options
                      else (_model_options[0] if _model_options else ""),
                label="DeepSeek Model",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The list refreshes daily from DeepSeek's own model catalogue — "
                "use Refresh below to check right now."
            )

        refresh_lbl = ui.label(
            f"Model list last refreshed: "
            f"{_fmt_last_refresh(cfg.get('ai_models_last_refreshed', 0))}"
        ).classes("text-xs text-gray-500")

        async def _refresh_models():
            refresh_lbl.text = "Refreshing model list..."
            models = await ai_provider.fetch_available_models("deepseek", api_key_in.value)
            deepseek_model_in.set_options(models)
            now = __import__("time").time()
            cfg_module.save_to_yaml({"deepseek_models_cache": models, "ai_models_last_refreshed": now})
            engine._cfg["deepseek_models_cache"]    = models
            engine._cfg["ai_models_last_refreshed"] = now
            refresh_lbl.text = f"Model list last refreshed: {_fmt_last_refresh(now)}"

        test_result = ui.label("").classes("text-sm mt-1")

        async def test_deepseek():
            test_result.text = "Testing..."
            test_result.classes(replace="text-sm mt-1 text-gray-400")
            try:
                test_cfg = {
                    "ai_provider": "deepseek",
                    "deepseek_api_key": api_key_in.value,
                    "deepseek_model": deepseek_model_in.value,
                }
                # Directive prompt (not bare "ping") — an open-ended prompt lets
                # the model write a full sentence and hit max_tokens before
                # finishing, even with thinking mode off; asking for one exact
                # word keeps this a real but small connectivity check.
                reply = await ai_provider.complete(
                    test_cfg, "", "Reply with just the word: pong", max_tokens=10, timeout=15
                )
                test_result.text = f"Connected — model: {deepseek_model_in.value}, reply: {reply[:40]!r}"
                test_result.classes(replace="text-sm mt-1 text-green-400")
            except Exception as e:
                test_result.text = f"Failed: {e}"
                test_result.classes(replace="text-sm mt-1 text-red-400")

        async def save_config():
            cfg_module.save_to_yaml({
                "deepseek_api_key": api_key_in.value,
                "deepseek_model":   deepseek_model_in.value,
            })
            engine._cfg["deepseek_api_key"] = api_key_in.value
            engine._cfg["deepseek_model"]   = deepseek_model_in.value
            # Unlike the Claude key above, the DeepSeek key IS synced to the VPS
            # — explicit user choice (2026-07-08), since the VPS had no DeepSeek
            # key of its own and syncing the provider choice alone would just
            # leave it unable to make any AI calls at all.
            await _push_ai_config_to_vps({
                "deepseek_api_key": api_key_in.value,
                "deepseek_model":   deepseek_model_in.value,
            })
            ui.notify("Config saved", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save Config", on_click=save_config).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Test Connection", on_click=test_deepseek).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
            ui.button("Refresh Models", icon="refresh", on_click=_refresh_models).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
        test_result


def _fmt_last_refresh(ts: float) -> str:
    if not ts:
        return "never"
    from datetime import datetime as _dt
    return _dt.fromtimestamp(ts).strftime("%d %b %H:%M")


def _render_tg_bot():
    tg_cfg = settings_ctl.get_telegram_config()
    cfg    = cfg_module.load()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Telegram Bot Alerts").classes("text-lg font-bold text-yellow-300 mb-4")
        ui.label(
            "These settings control the bot that SENDS trade notifications."
        ).classes("text-sm text-gray-400")

        with ui.row().classes("w-full items-center gap-1"):
            bot_token = ui.input(
                "Bot Token", value=tg_cfg.get("bot_token_enc", "") or "", password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Create a bot via @BotFather on Telegram. "
                "Token format: 1234567890:ABCDEF..."
            )

        with ui.row().classes("w-full items-center gap-1"):
            chat_id = ui.input("Chat ID", value=tg_cfg.get("chat_id", "") or "").classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The Telegram chat ID where alerts will be sent. "
                "Get this from @userinfobot or @RawDataBot."
            )

        with ui.row().classes("items-center gap-1"):
            enabled = ui.checkbox(
                "Enable Telegram alerts", value=bool(tg_cfg.get("enabled", 0))
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "When enabled, trade opens, closes, and TP hits are sent to the chat ID above."
            )

        def save_tg():
            settings_ctl.save_telegram_config(bot_token.value, chat_id.value, enabled.value)
            ui.notify("Telegram config saved", type="positive")

        async def test_tg():
            import backend.src.services.telegram.alerts as alerts
            ok = await alerts.send_message(
                "*FOREX Trader — Test Alert*\nTelegram alerts are working.",
                event_type="test",
            )
            ui.notify(
                "Sent!" if ok else "Failed — check bot token and chat ID",
                type="positive" if ok else "negative",
            )

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save", on_click=save_tg).classes("bg-blue-700 text-white px-4 py-2")
            ui.button("Test Alert", on_click=test_tg).classes(
                "bg-gray-600 text-white px-4 py-2"
            )

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-4 rounded-lg mt-4"):
        ui.label("Telegram Reader (Telethon)").classes("font-semibold text-yellow-300 mb-2")
        ui.label(
            "API credentials for reading signals. Change in config.yaml and restart."
        ).classes("text-sm text-gray-400")
        ui.label(
            f"API ID set:   {'Yes' if cfg.get('telegram_api_id') else 'No'}"
        ).classes("text-sm text-gray-300")
        ui.label(
            f"API Hash set: {'Yes' if cfg.get('telegram_api_hash') else 'No'}"
        ).classes("text-sm text-gray-300")
        ui.label(
            f"Phone set:    {'Yes' if cfg.get('telegram_phone') else 'No'}"
        ).classes("text-sm text-gray-300")

        with ui.row().classes("w-full items-center gap-1"):
            tg_api_id = ui.input(
                "API ID", value=str(cfg.get("telegram_api_id", "") or "")
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "From my.telegram.org/apps — your Telegram API application ID"
            )

        with ui.row().classes("w-full items-center gap-1"):
            tg_api_hash = ui.input(
                "API Hash", value=str(cfg.get("telegram_api_hash", "") or ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "From my.telegram.org/apps — your Telegram API application hash"
            )

        with ui.row().classes("w-full items-center gap-1"):
            tg_phone = ui.input(
                "Phone", value=str(cfg.get("telegram_phone", "") or "")
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Your Telegram account phone number in international format: +441234567890"
            )

        def save_tg_reader():
            cfg_module.save_to_yaml({
                "telegram_api_id":   tg_api_id.value,
                "telegram_api_hash": tg_api_hash.value,
                "telegram_phone":    tg_phone.value,
            })
            ui.notify("Saved to config.yaml — restart app for changes to take effect", type="info")

        ui.button("Save Reader Credentials", on_click=save_tg_reader).classes(
            "bg-blue-700 text-white mt-2 px-4 py-2"
        )


def _smtp_friendly_error(raw: str) -> str:
    """Translate raw SMTP exception strings into actionable user messages."""
    r = raw.lower()

    # Outlook/Microsoft 535 — basic auth disabled (Authenticated SMTP toggle not on)
    if "535" in r and ("basic authentication" in r or "authentication unsuccessful" in r or "5.7.139" in r):
        return (
            "✗  Microsoft rejected the login: SMTP AUTH (basic authentication) is disabled.\n\n"
            "FIX — enable Authenticated SMTP in your Outlook account:\n"
            "  1. Open outlook.live.com and sign in\n"
            "  2. Click Settings gear → View all Outlook settings\n"
            "  3. Go to Mail → Sync email\n"
            "  4. Toggle 'Authenticated SMTP' to ON and click Save\n"
            "  5. Come back here and click Send Test Email again\n\n"
            "This is a Microsoft account setting, not an app issue.\n"
            f"Original error: {raw[:120]}"
        )

    # Gmail 535 — wrong app password or 2FA not enabled
    if "535" in r and "gmail" in r:
        return (
            "✗  Gmail rejected the login.\n"
            "Make sure you are using an App Password (not your regular password) "
            "and that 2-Step Verification is enabled on your Google account.\n"
            f"Error: {raw[:120]}"
        )

    # Generic 535 authentication
    if "535" in r:
        return (
            "✗  Authentication failed (535) — wrong username or password.\n"
            "If you have 2FA / two-step verification, you MUST use an App Password, "
            "not your regular account password.\n"
            f"Error: {raw[:120]}"
        )

    # Connection refused / timeout
    if "connection refused" in r or "timed out" in r or "network" in r:
        return (
            "✗  Could not connect to the mail server.\n"
            "Check the SMTP host and port are correct, and that your firewall "
            "allows outbound connections on port 587.\n"
            f"Error: {raw[:120]}"
        )

    # Certificate error
    if "certificate" in r or "ssl" in r:
        return (
            "✗  SSL/TLS certificate error.\n"
            "Try enabling 'Use TLS / STARTTLS encryption' if it is off, "
            "or switch to port 587 (STARTTLS) instead of 465.\n"
            f"Error: {raw[:120]}"
        )

    # Fallback
    return f"✗  Failed: {raw}"


def _render_email():
    ecfg = settings_ctl.get_email_config()

    # ── Schedule & Delivery card (top) ────────────────────────────────────────
    _SEND_PROVIDERS = {
        "resend":  "Resend (API — recommended)",
        "gmail":   "Gmail (SMTP)",
        "outlook": "Outlook.com (SMTP)",
        "custom":  "Custom SMTP",
    }

    def _default_send_provider() -> str:
        stored = ecfg.get("send_provider") or ""
        if stored in _SEND_PROVIDERS:
            return stored
        # Auto-detect from what is configured
        if (ecfg.get("resend_api_key") or "").strip():
            return "resend"
        h = (ecfg.get("smtp_host") or "").lower()
        if "gmail" in h:
            return "gmail"
        if "outlook" in h or "office365" in h:
            return "outlook"
        return "resend"

    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg mb-4"):
        ui.label("Schedule & Delivery").classes("text-lg font-bold text-yellow-400 mb-1")
        ui.label(
            "Choose which provider sends the reports, then set the schedule."
        ).classes("text-sm text-gray-400 mb-4")

        ui.label("Send reports via").classes("text-sm font-semibold text-gray-300 mb-1")
        with ui.row().classes("w-full items-center gap-1"):
            send_provider_sel = ui.select(
                _SEND_PROVIDERS,
                value=_default_send_provider(),
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Select which configured provider is used for scheduled daily and weekly reports. "
                "Configure the provider's credentials in the section below, then come back here to save."
            )

        ui.separator().classes("my-4")

        ui.label("Reports").classes("text-sm font-semibold text-gray-300 mb-2")
        with ui.row().classes("gap-6 flex-wrap"):
            with ui.row().classes("items-center gap-1"):
                daily_enabled = ui.checkbox(
                    "Daily summary",
                    value=bool(ecfg.get("daily_enabled", 0)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends a performance email every day at the time below."
                )

            with ui.row().classes("items-center gap-1"):
                weekly_enabled = ui.checkbox(
                    "Weekly summary (Fridays only)",
                    value=bool(ecfg.get("weekly_enabled", 0)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends a weekly summary every Friday. On Fridays you receive both "
                    "a daily and a weekly email as separate messages."
                )

            with ui.row().classes("items-center gap-1"):
                orb_enabled = ui.checkbox(
                    "Morning ORB / IVB report (08:15 UK time)",
                    value=bool(ecfg.get("orb_report_enabled", 1)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends the Asian session range (00:00-08:00 UTC) with a "
                    "volume-profile overlay (POC/value area), reload-zone entry, stop, "
                    "and target — plus a chart image — every weekday at 08:15 "
                    "Europe/London, shortly after London opens and the breakout has a "
                    "moment to establish direction (adjusts for BST/GMT automatically), "
                    "independent of the send time below."
                )

        with ui.row().classes("items-center gap-1 mt-2"):
            send_time = ui.input(
                "Send time (24-hour, HH:MM)",
                value=ecfg.get("send_time", "18:00") or "18:00",
            ).classes("w-40")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Time of day to send reports. Uses the server's local clock. "
                "18:00 = 6 PM.  The app checks the time every minute."
            )

        def save_schedule():
            settings_ctl.save_email_config({
                "send_provider":      send_provider_sel.value or "resend",
                "daily_enabled":      int(daily_enabled.value),
                "weekly_enabled":     int(weekly_enabled.value),
                "send_time":          send_time.value or "18:00",
                "orb_report_enabled": int(orb_enabled.value),
            })
            ui.notify("Schedule saved", type="positive")

        schedule_test_lbl = ui.label("").classes("text-sm mt-3")

        async def test_scheduled_email():
            from backend.src.services.notifications import email_service
            provider = send_provider_sel.value or "resend"
            fresh = settings_ctl.get_email_config()
            to_addr = (fresh.get("to_addr") or "").strip()
            if not to_addr:
                schedule_test_lbl.text = "Set 'Send reports to' in the SMTP section below first"
                schedule_test_lbl.classes(replace="text-sm text-orange-400")
                return
            # Pass full DB config but force selected provider so routing is correct
            fresh["send_provider"] = provider
            html = """<html><body style="background:#111827;padding:24px;font-family:Arial;">
            <p style="color:#f59e0b;font-size:22px;font-weight:bold;">FOREX Trader</p>
            <p style="color:#e5e7eb;">Delivery test — scheduled reports will be sent via this provider.</p>
            </body></html>"""
            schedule_test_lbl.text = f"Sending via {provider}..."
            schedule_test_lbl.classes(replace="text-sm text-gray-400")
            ok, err = await email_service.send_email("FOREX Trader — Delivery Test", html, fresh)
            if ok:
                schedule_test_lbl.text = f"Sent via {provider} to {to_addr}"
                schedule_test_lbl.classes(replace="text-sm text-green-400 font-semibold")
                ui.notify(f"Test sent via {provider}", type="positive")
            else:
                schedule_test_lbl.text = f"Failed: {err}"
                schedule_test_lbl.classes(replace="text-sm text-red-300")

        async def test_orb_email():
            from datetime import datetime as _dt
            from backend.src.services.notifications import email_service
            fresh = settings_ctl.get_email_config()
            to_addr = (fresh.get("to_addr") or "").strip()
            if not to_addr:
                schedule_test_lbl.text = "Set 'Send reports to' in the SMTP section below first"
                schedule_test_lbl.classes(replace="text-sm text-orange-400")
                return
            schedule_test_lbl.text = "Building ORB report..."
            schedule_test_lbl.classes(replace="text-sm text-gray-400")
            from backend.src.app import get_engine as _get_engine
            engine = _get_engine()
            report = await engine.build_orb_report()
            if not report:
                schedule_test_lbl.text = "Could not build report — MT5 bridge/candles not available right now"
                schedule_test_lbl.classes(replace="text-sm text-red-300")
                return
            chart_png = email_service.build_orb_chart_image(report)
            html = email_service.build_orb_html(
                report, _dt.now().strftime("%A, %d %B %Y"),
                has_chart=bool(chart_png),
            )
            ok, err = await email_service.send_email(
                "FOREX Trader — London Open ORB Report (test)", html, fresh,
                image_bytes=chart_png, image_cid=email_service._ORB_CHART_CID,
            )
            if ok:
                schedule_test_lbl.text = f"ORB test sent to {to_addr}"
                schedule_test_lbl.classes(replace="text-sm text-green-400 font-semibold")
                ui.notify("ORB test email sent", type="positive")
            else:
                schedule_test_lbl.text = f"Failed: {err}"
                schedule_test_lbl.classes(replace="text-sm text-red-300")

        with ui.row().classes("gap-3 mt-4"):
            ui.button("Save Schedule", icon="save", on_click=save_schedule).classes(
                "bg-blue-700 text-white px-5 py-2"
            )
            ui.button("Test Delivery", icon="send", on_click=test_scheduled_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
            ui.button("Send Test ORB Report", icon="send", on_click=test_orb_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
        schedule_test_lbl

    # ── Resend card ───────────────────────────────────────────────────────────
    with ui.card().classes("w-full max-w-2xl rounded-lg p-0 overflow-hidden mb-4").style(
        "border:2px solid #6366f1;"
    ):
        with ui.row().classes("w-full px-4 py-2 items-center gap-2").style(
            "background:#0e0e2a;"
        ):
            ui.label("Resend API").classes("text-sm font-bold text-indigo-300")
            ui.badge("NO SMTP NEEDED", color="indigo").classes("text-xs")
            ui.label("3,000 emails/month free · no domain required").classes(
                "text-xs text-indigo-400 ml-auto"
            )

        with ui.column().classes("px-5 py-4 gap-3 bg-gray-800 w-full"):
            ui.label(
                "Resend uses a secure HTTPS API — no app passwords, no SMTP ports. "
                "Works immediately with your email as the recipient. "
                "Get an API key in under 2 minutes at resend.com (free, no credit card)."
            ).classes("text-xs text-gray-300 leading-relaxed")

            with ui.expansion("Setup steps (2 min)", icon="help_outline").classes(
                "w-full bg-gray-700 rounded text-xs"
            ):
                with ui.column().classes("p-3 gap-1"):
                    for step in [
                        "1.  Go to  resend.com  and click  Get started for free",
                        "2.  Sign up with your email — no credit card required",
                        "3.  Go to  API Keys  in the left sidebar",
                        "4.  Click  Create API Key  — give it any name (e.g. FOREX Trader)",
                        "5.  Copy the key (starts with re_) and paste it below",
                        "6.  Set your own email as the 'Send reports to' address in the SMTP section",
                        "Note: emails arrive from  onboarding@resend.dev unless you verify",
                        "      your own domain at  resend.com/domains  (optional).",
                    ]:
                        ui.label(step).classes("text-xs text-gray-200 font-mono leading-relaxed")

            with ui.row().classes("w-full items-center gap-1"):
                rs_key = ui.input(
                    "Resend API Key  (starts with re_...)",
                    value=ecfg.get("resend_api_key", "") or "",
                    password=True,
                ).classes("flex-1")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Your Resend API key — find it at resend.com → API Keys"
                )

            rs_result = ui.label("").classes("text-sm")

            async def test_resend():
                rs_result.text = "Testing Resend..."
                rs_result.classes(replace="text-sm text-gray-400")
                from backend.src.services.notifications import email_service
                _fresh = settings_ctl.get_email_config()
                cfg_snap = {
                    "resend_api_key": rs_key.value,
                    "to_addr": _fresh.get("to_addr") or _fresh.get("smtp_user") or "",
                    "from_addr": _fresh.get("from_addr") or "",
                }
                if not cfg_snap["to_addr"]:
                    rs_result.text = "Set your 'Send reports to' address in the SMTP section below first"
                    rs_result.classes(replace="text-sm text-orange-400")
                    return
                html = """<html><body style="background:#111827;padding:24px;font-family:Arial;">
                <p style="color:#6366f1;font-size:22px;font-weight:bold;">FOREX Trader</p>
                <p style="color:#e5e7eb;">Resend connection test successful.</p>
                </body></html>"""
                ok, err = await email_service.send_email(
                    "FOREX Trader — Resend Test", html, cfg_snap
                )
                if ok:
                    rs_result.text = f"Sent via Resend to {cfg_snap['to_addr']}"
                    rs_result.classes(replace="text-sm text-green-400 font-semibold")
                    ui.notify("Resend test sent!", type="positive")
                else:
                    rs_result.text = f"Failed: {err}"
                    rs_result.classes(replace="text-sm text-red-300")

            def save_resend():
                settings_ctl.save_email_config({"resend_api_key": rs_key.value or ""})
                ui.notify("Resend API key saved", type="positive")

            with ui.row().classes("gap-3 mt-1"):
                ui.button("Save Key", icon="save", on_click=save_resend).classes(
                    "bg-indigo-700 text-white px-4 py-2"
                )
                ui.button("Test Resend", icon="send", on_click=test_resend).classes(
                    "bg-gray-600 text-white px-4 py-2"
                )
            rs_result

    ui.label("— or use SMTP directly —").classes(
        "text-xs text-gray-500 text-center w-full max-w-2xl my-1"
    )

    # ── Provider presets ──────────────────────────────────────────────────────
    _PROVIDERS = {
        "gmail":   {
            "label":    "Gmail",
            "host":     "smtp.gmail.com",
            "port":     587,
            "tls":      True,
            "pw_label": "App Password",
            "username_hint": "your.address@gmail.com",
        },
        "outlook": {
            "label":    "Outlook.com (personal @outlook / @hotmail)",
            "host":     "smtp-mail.outlook.com",
            "port":     587,
            "tls":      True,
            "pw_label": "App Password",
            "username_hint": "your.address@outlook.com",
        },
        "custom":  {
            "label":    "Custom / Other",
            "host":     "",
            "port":     587,
            "tls":      True,
            "pw_label": "Password",
            "username_hint": "your@email.com",
        },
    }

    def _detect_provider() -> str:
        h = (ecfg.get("smtp_host") or "").lower()
        if "gmail" in h:   return "gmail"
        if "office365" in h or "outlook.com" in h: return "outlook"
        return "custom"

    _sel_provider = [_detect_provider()]

    # ── SMTP card ─────────────────────────────────────────────────────────────
    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        ui.label("SMTP Settings").classes("text-lg font-bold text-yellow-400 mb-1")
        ui.label(
            "Configure Gmail or Outlook SMTP credentials. "
            "Address settings here are also used by Resend as the recipient."
        ).classes("text-sm text-gray-400 mb-4")

        # ── Provider selector ─────────────────────────────────────────────────
        ui.label("Email Provider").classes("text-sm font-semibold text-gray-300 mb-1")
        with ui.row().classes("w-full items-center gap-1"):
            provider_sel = ui.select(
                {k: v["label"] for k, v in _PROVIDERS.items()},
                value=_sel_provider[0],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Select your email provider. Settings are filled in automatically. "
                "Choose Custom to enter your own SMTP server details."
            )

        ui.separator().classes("my-3")

        # ── How-to card (shown for Gmail and Outlook) ─────────────────────────
        howto_card = ui.card().classes("w-full rounded-lg p-4 mb-1").style(
            "background:#1c1a0e; border:1px solid #d97706;"
        )

        def _build_howto(provider: str):
            howto_card.clear()
            with howto_card:
                if provider == "gmail":
                    ui.label("How to get a Gmail App Password").classes(
                        "text-sm font-bold text-yellow-300 mb-2"
                    )
                    steps = [
                        "1.  Go to  myaccount.google.com",
                        "2.  Click  Security  in the left sidebar",
                        "3.  Under 'How you sign in to Google', open  2-Step Verification",
                        "     (enable it first if not already on)",
                        "4.  Scroll to the bottom of that page and click  App passwords",
                        "5.  Choose app: Mail  →  device: Other → type 'FOREX Trader'",
                        "6.  Click  Generate — copy the 16-character password shown",
                        "7.  Paste it into the App Password field below",
                    ]
                    for s in steps:
                        ui.label(s).classes(
                            "text-xs text-gray-200 font-mono leading-relaxed"
                        )
                    ui.label(
                        "Note: the App Password is shown only once. "
                        "You can revoke and regenerate it at any time."
                    ).classes("text-xs text-gray-400 mt-2 italic")

                elif provider == "outlook":
                    ui.label("Outlook.com personal — Two mandatory steps").classes(
                        "text-sm font-bold text-yellow-300 mb-2"
                    )

                    with ui.row().classes("w-full items-start gap-2 p-3 rounded mb-3").style(
                        "background:#3b0a0a; border:2px solid #ef4444;"
                    ):
                        ui.label("warning").classes("material-icons text-red-400 text-base shrink-0 mt-0.5")
                        with ui.column().classes("gap-0.5"):
                            ui.label(
                                "Error 535 'basic authentication is disabled'?"
                            ).classes("text-xs font-bold text-red-300")
                            ui.label(
                                "Microsoft disables SMTP AUTH by default. You MUST enable the "
                                "'Authenticated SMTP' toggle in Outlook settings (Step 1 below) "
                                "— this is separate from TLS encryption and from the App Password. "
                                "Without it, even a correct App Password is rejected at the server."
                            ).classes("text-xs text-red-200 leading-relaxed")

                    with ui.row().classes("items-start gap-2 mb-3"):
                        ui.label("1").classes(
                            "text-xs font-bold bg-red-600 text-white rounded-full "
                            "w-5 h-5 flex items-center justify-center shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Enable Authenticated SMTP  (fixes error 535)").classes(
                                "text-xs font-semibold text-red-300"
                            )
                            for s in [
                                "a.  Open  outlook.live.com  in a browser and sign in",
                                "b.  Click the Settings gear  →  View all Outlook settings",
                                "c.  Go to  Mail  →  Sync email",
                                "d.  Scroll to the  'POP and IMAP'  section",
                                "e.  Toggle  Authenticated SMTP  to ON (blue)",
                                "f.  Click  Save  — it takes effect immediately",
                            ]:
                                ui.label(s).classes("text-xs text-gray-200 font-mono")
                            ui.label(
                                "Direct link: outlook.live.com → Settings → Mail → Sync email"
                            ).classes("text-xs text-blue-400 mt-1 italic")

                    with ui.row().classes("items-start gap-2"):
                        ui.label("2").classes(
                            "text-xs font-bold bg-yellow-600 text-black rounded-full "
                            "w-5 h-5 flex items-center justify-center shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Create an App Password  (if 2FA is enabled on your account)").classes(
                                "text-xs font-semibold text-white"
                            )
                            for s in [
                                "a.  Go to  account.microsoft.com",
                                "b.  Click  Security  in the top nav",
                                "c.  Under 'Advanced security options' click  Get started",
                                "d.  Scroll to  App passwords  →  Create a new app password",
                                "e.  Copy the password and paste it in the App Password field below",
                            ]:
                                ui.label(s).classes("text-xs text-gray-200 font-mono")
                            ui.label(
                                "App Password is shown only once — save it. "
                                "Without 2FA you can use your normal account password."
                            ).classes("text-xs text-gray-400 mt-1 italic")

                else:
                    howto_card.style("display:none")

        _build_howto(_sel_provider[0])
        if _sel_provider[0] == "custom":
            howto_card.style("display:none")

        # ── SMTP fields ───────────────────────────────────────────────────────
        ui.label("Connection").classes("text-sm font-semibold text-gray-300 mb-1 mt-3")

        with ui.row().classes("w-full items-center gap-2"):
            smtp_host = ui.input(
                "SMTP Host",
                value=ecfg.get("smtp_host", "") or _PROVIDERS[_sel_provider[0]]["host"],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The outgoing mail server address for your provider."
            )

            smtp_port = ui.number(
                "Port",
                value=int(ecfg.get("smtp_port") or _PROVIDERS[_sel_provider[0]]["port"]),
                min=1, max=65535, step=1, format="%.0f",
            ).classes("w-24")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "587 = STARTTLS (recommended).  465 = SSL.  25 = plain (avoid)."
            )

        with ui.row().classes("items-center gap-1"):
            use_tls = ui.checkbox(
                "Use TLS / STARTTLS encryption (strongly recommended)",
                value=bool(ecfg.get("use_tls", 1)),
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Must be on for Gmail and Outlook. Encrypts the connection so your "
                "password is not sent in plain text."
            )

        ui.separator().classes("my-3")
        ui.label("Account").classes("text-sm font-semibold text-gray-300 mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            smtp_user = ui.input(
                "Email address (username)",
                value=ecfg.get("smtp_user", "") or "",
                placeholder=_PROVIDERS[_sel_provider[0]]["username_hint"],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Your full email address — this is used as the login username."
            )

        with ui.row().classes("w-full items-center gap-2 mt-1"):
            ui.label("🔑").classes("text-base shrink-0")
            pw_title = ui.label(
                _PROVIDERS[_sel_provider[0]]["pw_label"]
            ).classes("text-sm font-semibold text-yellow-300 shrink-0")
            pw_badge = ui.badge(
                "recommended" if _sel_provider[0] != "custom" else "",
                color="amber",
            ).classes("text-xs shrink-0")
            if _sel_provider[0] == "custom":
                pw_badge.style("display:none")

        smtp_password = ui.input(
            "",
            password=True,
            value=ecfg.get("smtp_password", "") or "",
            placeholder="Paste your App Password here (leave blank to keep existing)",
        ).classes("w-full")

        pw_hint = ui.label("").classes("text-xs text-gray-400 mt-1")

        def _set_pw_hint(provider: str):
            hints = {
                "gmail":   ("An App Password is a 16-character code generated at "
                            "myaccount.google.com. It is NOT your regular Google password."),
                "outlook": ("An App Password is generated at account.microsoft.com > Security. "
                            "It is NOT your regular Microsoft password."),
                "custom":  "Enter your regular SMTP password for this account.",
            }
            pw_hint.text = hints.get(provider, "")

        _set_pw_hint(_sel_provider[0])

        ui.separator().classes("my-3")
        ui.label("Addresses").classes("text-sm font-semibold text-gray-300 mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            from_addr = ui.input(
                "From address",
                value=ecfg.get("from_addr", "") or "",
                placeholder="Same as your email address above",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The sender address shown in the email. "
                "Must match your SMTP account address for most providers."
            )

        with ui.row().classes("w-full items-center gap-1"):
            to_addr = ui.input(
                "Send reports to",
                value=ecfg.get("to_addr", "") or "",
                placeholder="Where you want to receive the reports",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Reports are delivered here. Can be the same as your From address "
                "or a different inbox."
            )

        # ── Provider change handler ───────────────────────────────────────────
        def _on_provider_change(e):
            p = e.value
            _sel_provider[0] = p
            preset = _PROVIDERS[p]
            smtp_host.value = preset["host"]
            smtp_port.value = preset["port"]
            use_tls.value   = preset["tls"]
            smtp_user.placeholder = preset["username_hint"]
            pw_title.text = preset["pw_label"]
            _set_pw_hint(p)
            if p != "custom":
                pw_badge.style("")
                pw_badge.text = "recommended"
            else:
                pw_badge.style("display:none")
            _build_howto(p)
            if p == "custom":
                howto_card.style("display:none")
            else:
                howto_card.style("background:#1c1a0e; border:1px solid #d97706;")

        provider_sel.on("update:model-value", _on_provider_change)

        # ── Buttons ───────────────────────────────────────────────────────────
        test_result = ui.label("").classes("text-sm mt-3 leading-relaxed")

        async def test_email():
            test_result.text = "Connecting to SMTP server..."
            test_result.classes(replace="text-sm mt-3 text-gray-400 leading-relaxed")
            try:
                from backend.src.services.notifications import email_service
                cfg_snap = {
                    "smtp_host":     smtp_host.value,
                    "smtp_port":     int(smtp_port.value or 587),
                    "smtp_user":     smtp_user.value,
                    "smtp_password": smtp_password.value or ecfg.get("smtp_password", ""),
                    "from_addr":     from_addr.value or smtp_user.value,
                    "to_addr":       to_addr.value,
                    "use_tls":       int(use_tls.value),
                }
                html = """<!DOCTYPE html><html><body
                  style="margin:0;padding:24px;background:#111827;font-family:Arial,sans-serif;">
                <p style="color:#f59e0b;font-size:22px;font-weight:bold;">FOREX Trader</p>
                <p style="color:#e5e7eb;font-size:15px;margin-top:12px;">
                  Your email settings are working correctly.
                  Daily and weekly trade summaries will be delivered to this address.
                </p>
                <p style="color:#6b7280;font-size:12px;margin-top:20px;">
                  This is a test message sent from FOREX Trader Email Reports.
                </p></body></html>"""
                ok, err = await email_service.send_email(
                    "FOREX Trader — Connection Test", html, cfg_snap
                )
                if ok:
                    test_result.text = (
                        f"✓  Test email sent successfully to {to_addr.value or smtp_user.value}"
                    )
                    test_result.classes(replace="text-sm mt-3 text-green-400 font-semibold leading-relaxed")
                    ui.notify("Test email sent!", type="positive")
                else:
                    friendly = _smtp_friendly_error(err)
                    test_result.text = friendly
                    test_result.classes(replace="text-sm mt-3 text-red-300 leading-relaxed whitespace-pre-line")
            except Exception as exc:
                friendly = _smtp_friendly_error(str(exc))
                test_result.text = friendly
                test_result.classes(replace="text-sm mt-3 text-red-400 leading-relaxed whitespace-pre-line")

        def save_smtp():
            updates: dict = {
                "smtp_host":  smtp_host.value or "",
                "smtp_port":  int(smtp_port.value or 587),
                "smtp_user":  smtp_user.value or "",
                "from_addr":  from_addr.value or smtp_user.value or "",
                "to_addr":    to_addr.value or "",
                "use_tls":    int(use_tls.value),
            }
            if smtp_password.value:
                updates["smtp_password"] = smtp_password.value
            settings_ctl.save_email_config(updates)
            ui.notify("SMTP settings saved", type="positive")

        with ui.row().classes("gap-3 mt-4 flex-wrap"):
            ui.button("Save Settings", icon="save", on_click=save_smtp).classes(
                "bg-blue-700 text-white px-5 py-2"
            )
            ui.button("Send Test Email", icon="send", on_click=test_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
        test_result


_bridge_proc     = [None]   # holds the Popen for the bridge subprocess
_bridge_starting = [False]  # True while start_bridge() is in the startup window


def _wine_bin() -> str:
    """Wine binary — reads from config (wine_bin key).
    Defaults to CrossOver's binary which ships on this machine; update config.yaml
    to /opt/homebrew/bin/wine after installing a system Wine via Homebrew."""
    import backend.src.config as _cfg
    return (_cfg.get("wine_bin") or "").strip() or \
        "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"


def _mt5_bottle_path() -> str:
    """Wine prefix (WINEPREFIX) for MT5.
    After running setup_wine_bridge.sh this is ~/.wine_mt5 — an independent
    prefix that does not require CrossOver to be installed."""
    import os as _os
    import backend.src.config as _cfg
    return (_cfg.get("mt5_bottle_path") or "").strip() or \
        _os.path.expanduser("~/.wine_mt5")


def _bridge_running() -> bool:
    return _pu.is_port_listening(9000)


def _render_bridge_control(engine):
    import subprocess, os

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mb-4"):
        ui.label("MT5 Bridge Control").classes("font-bold text-yellow-300 mb-3")

        _cfg_now = cfg_module.load()

        # ── Backend selector (macOS only — Wine/CrossOver not needed on Windows) ─
        if sys.platform != "win32":
            ui.label("Backend").classes("text-sm font-semibold text-gray-300 mb-1")
            with ui.row().classes("items-center gap-3 mb-1"):
                backend_sel = ui.select(
                    {
                        "crossover": "CrossOver",
                        "wine":      "Wine (independent prefix)",
                    },
                    value=_cfg_now.get("bridge_backend", "crossover"),
                ).classes("w-56")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "CrossOver: uses the CrossOver-managed bottle. Known working on this machine.\n"
                    "Wine: uses an independent prefix at mt5_bottle_path "
                    "(run setup_wine_bridge.sh first)."
                )

                def _save_backend():
                    cfg_module.save_to_yaml({"bridge_backend": backend_sel.value})
                    _update_backend_hint()
                    ui.notify(f"Backend set to {backend_sel.value}", type="positive")

                ui.button("Save", icon="save", on_click=_save_backend).classes(
                    "bg-blue-700 text-white text-xs px-3 py-1"
                )

            backend_hint = ui.label("").classes("text-xs text-gray-500 mb-3")

            def _update_backend_hint():
                if backend_sel.value == "crossover":
                    backend_hint.text = (
                        "Bottle: ~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
                    )
                else:
                    backend_hint.text = (
                        f"Prefix: {_cfg_now.get('mt5_bottle_path') or os.path.expanduser('~/.wine_mt5')}"
                    )

            _update_backend_hint()
            backend_sel.on("update:model-value", lambda _: _update_backend_hint())
        else:
            # Windows: no Wine needed — bridge runs natively
            backend_sel = type("_Stub", (), {"value": "native"})()  # stub so start_bridge() can check
            ui.label("Native Windows bridge — no Wine or CrossOver required.").classes(
                "text-xs text-green-400 mb-3"
            )

        ui.separator().classes("mb-3")

        # MT5 Bridge URL
        with ui.row().classes("w-full items-center gap-1 mb-2"):
            bridge_url_inp = ui.input(
                "MT5 Bridge URL",
                value=_cfg_now.get("mt5_bridge_url", "http://localhost:9000"),
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "HTTP address where mt5_bridge.py is listening. "
                "Default is http://localhost:9000 — change only if running the bridge remotely."
            )

        def _save_bridge_url():
            cfg_module.save_to_yaml({"mt5_bridge_url": bridge_url_inp.value.strip()})
            ui.notify("Bridge URL saved", type="positive")

        ui.button("Save URL", icon="save", on_click=_save_bridge_url).classes(
            "bg-blue-700 text-white text-xs px-3 py-1 mb-3"
        )

        ui.separator().classes("mb-3")

        bridge_status_lbl = ui.label("").classes("text-sm mb-2")
        bridge_log_lbl    = ui.label("").classes("text-xs text-gray-500 font-mono whitespace-pre-wrap")

        def _refresh_status():
            running = _bridge_running()
            bridge_status_lbl.text = (
                "Bridge: RUNNING on port 9000" if running
                else "Bridge: NOT running — start it before connecting"
            )
            bridge_status_lbl.classes(
                replace="text-sm mb-2 " + ("text-green-400" if running else "text-red-400")
            )

        _refresh_status()

        async def start_bridge():
            import asyncio
            import pathlib as _pl
            # Re-enable auto-reconnect whenever the user manually starts the bridge
            try:
                engine.set_bridge_inhibit_reconnect(False)
            except Exception:
                pass
            if _bridge_running() or _bridge_starting[0]:
                bridge_log_lbl.text = "Bridge is already running or starting."
                return
            _bridge_starting[0] = True

            # Bridge script lives in the FOREX project root
            from backend.src.utils.os_utils import repo_root as _repo_root
            _forex_root   = _repo_root()
            _bridge_macos = _forex_root / "mt5_bridge.py"
            if not _bridge_macos.exists():
                bridge_log_lbl.text = (
                    f"mt5_bridge.py not found.\nExpected: {_bridge_macos}\n"
                    "Run setup_wine_bridge.sh first."
                )
                bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
                return

            env = cfg_module.get("account_env", "demo")
            ok  = settings_ctl.sync_bridge_credentials_file(env)
            if not ok:
                bridge_log_lbl.text = (
                    f"Cannot start — no credentials for {env} env.\n"
                    "Save credentials in Settings > MT5 / Bridge first."
                )
                bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
                return

            try:
                # ── Windows: run bridge natively without Wine ─────────────────
                if sys.platform == "win32":
                    from backend.src.config import USER_DATA_DIR
                    from urllib.parse import urlparse as _urlparse
                    _creds_path  = str(USER_DATA_DIR / "bridge_credentials.json")
                    _bridge_port = _urlparse(cfg_module.get("mt5_bridge_url", "")).port or 9010
                    _env_vars   = {
                        **os.environ,
                        "MT5_BRIDGE_PORT":   str(_bridge_port),
                        "BRIDGE_CREDS_PATH": _creds_path,
                    }
                    proc = subprocess.Popen(
                        [sys.executable, str(_bridge_macos)],
                        env=_env_vars,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    _bridge_proc[0] = proc
                    bridge_status_lbl.text = "Bridge starting — please wait..."
                    bridge_status_lbl.classes(replace="text-sm mb-2 text-yellow-400")
                    bridge_log_lbl.text = (
                        f"Bridge process launched (PID {proc.pid}) — native Windows mode.\n"
                        f"Script: {_bridge_macos}\n"
                        f"Credentials: {_creds_path}\n"
                        "Waiting 5 s for MT5 connection..."
                    )
                    bridge_log_lbl.classes(replace="text-xs text-gray-400 font-mono whitespace-pre-wrap")
                    await asyncio.sleep(5)
                    if proc.poll() is not None:
                        try:
                            out = proc.stdout.read(4096).decode(errors="replace").strip()
                        except Exception:
                            out = "(no output)"
                        bridge_log_lbl.text = (
                            f"Bridge exited immediately (code {proc.returncode}).\n\n"
                            f"Output:\n{out or '(none)'}\n\n"
                            "Common causes:\n"
                            "  - MetaTrader5 package not installed (pip install MetaTrader5)\n"
                            "  - MT5 terminal not running or not logged in\n"
                            "  - Algo Trading not enabled in MT5 toolbar"
                        )
                        bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
                    _refresh_status()
                    _bridge_starting[0] = False
                    return

                # ── macOS: Wine/CrossOver path ────────────────────────────────
                # Check for multiple MT5 instances before starting the bridge
                try:
                    _mt5_pids = subprocess.check_output(
                        ["pgrep", "-f", "terminal64.exe"],
                        stderr=subprocess.DEVNULL,
                    ).decode().strip().split()
                    if len(_mt5_pids) > 1:
                        bridge_log_lbl.text = (
                            f"Warning: {len(_mt5_pids)} MetaTrader 5 instances detected "
                            f"(PIDs: {', '.join(_mt5_pids)}).\n\n"
                            "Multiple MT5 instances can cause conflicts. "
                            "Please close the extra instance(s) in CrossOver before "
                            "starting the bridge."
                        )
                        bridge_log_lbl.classes(
                            replace="text-xs text-orange-400 font-mono whitespace-pre-wrap"
                        )
                        return
                except subprocess.CalledProcessError:
                    pass  # pgrep returns non-zero when no processes found — this is fine

                _wine    = _wine_bin()
                _backend = backend_sel.value  # "crossover" or "wine"

                if _backend == "crossover":
                    _bottle     = os.path.expanduser(
                        "~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
                    )
                    _extra_env = {
                        "CX_BOTTLE":     _bottle,
                        "CX_NO_BROWSER": "1",
                    }
                else:
                    _bottle    = _mt5_bottle_path()
                    _extra_env = {}

                if not os.path.isdir(_bottle):
                    bridge_log_lbl.text = (
                        f"Wine prefix not found:\n{_bottle}\n"
                        + (
                            "Is CrossOver installed?"
                            if _backend == "crossover"
                            else "Run setup_wine_bridge.sh to create the prefix."
                        )
                    )
                    bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
                    return

                # Verify Python 3.11 is installed in the bottle before launching
                _python_host = os.path.join(_bottle, "drive_c", "Python311", "python.exe")
                if not os.path.isfile(_python_host):
                    bridge_log_lbl.text = (
                        f"Python 3.11 not found in the Wine bottle.\n"
                        f"Expected: {_python_host}\n\n"
                        "Click 'Setup Bridge Dependencies' to install it automatically\n"
                        "(one-time setup, 2-5 minutes)."
                    )
                    bridge_log_lbl.classes(replace="text-xs text-orange-400 font-mono whitespace-pre-wrap")
                    return

                # Convert macOS path to Wine Z: path (Wine maps / as Z:\)
                _bridge_win = "Z:" + str(_bridge_macos).replace("/", "\\")
                # Build the Z: path so Wine Python can read the credentials
                # file from this checkout's own USER_DATA_DIR (config.py) --
                # NOT the live app's shared ~/Library/Application Support/
                # ForexTrader/ folder.
                import backend.src.config as _cfg_mod
                _mac_creds = str(_cfg_mod.USER_DATA_DIR / "bridge_credentials.json")
                _win_creds = "Z:" + _mac_creds.replace("/", "\\")
                from urllib.parse import urlparse as _urlparse
                _bridge_port = _urlparse(_cfg_mod.get("mt5_bridge_url", "")).port or 9010
                env_vars = {
                    **os.environ,
                    "WINEPREFIX":        _bottle,
                    "WINEDEBUG":         "-all",
                    "MT5_BRIDGE_PORT":   str(_bridge_port),
                    "BRIDGE_CREDS_PATH": _win_creds,
                    **_extra_env,
                }
                cmd = [_wine, "C:\\Python311\\python.exe", _bridge_win]
                proc = subprocess.Popen(
                    cmd,
                    env=env_vars,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                _bridge_proc[0] = proc
                bridge_status_lbl.text = "Bridge starting — please wait..."
                bridge_status_lbl.classes(replace="text-sm mb-2 text-yellow-400")
                bridge_log_lbl.text = (
                    f"Bridge process launched (PID {proc.pid}) via {_backend}.\n"
                    f"Wine: {_wine}\n"
                    f"Bottle: {_bottle}\n"
                    f"Script: {_bridge_win}\n"
                    "Waiting 10 s for MT5 connection..."
                )
                bridge_log_lbl.classes(replace="text-xs text-gray-400 font-mono whitespace-pre-wrap")

                # Give the bridge time to start before checking port 9000
                await asyncio.sleep(10)

                # If the process exited immediately, show its output as the error
                if proc.poll() is not None:
                    try:
                        out = proc.stdout.read(4096).decode(errors="replace").strip()
                    except Exception:
                        out = "(no output)"
                    bridge_log_lbl.text = (
                        f"Bridge exited immediately (code {proc.returncode}).\n\n"
                        f"Output:\n{out or '(none)'}\n\n"
                        "Common causes:\n"
                        "  - Python311 not installed in the Wine bottle\n"
                        "  - MetaTrader5 package missing (pip install MetaTrader5)\n"
                        "  - Wrong bottle path or CrossOver not configured"
                    )
                    bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")

                _refresh_status()
            except Exception as e:
                bridge_log_lbl.text = f"Failed to start bridge: {e}"
                bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
            finally:
                _bridge_starting[0] = False

        def stop_bridge():
            # Tell the engine watchdog not to auto-reconnect after a manual stop
            try:
                engine.set_bridge_inhibit_reconnect(True)
            except Exception:
                pass
            pids = _pu.pids_listening_on(9000)
            if pids:
                for pid in pids:
                    _pu.kill_pid(pid)
                bridge_log_lbl.text = (
                    f"Termination signal sent to bridge PID(s): {', '.join(map(str, pids))}"
                )
                bridge_log_lbl.classes(replace="text-xs text-yellow-400 font-mono whitespace-pre-wrap")
            else:
                bridge_log_lbl.text = "Bridge is not running."
                bridge_log_lbl.classes(replace="text-xs text-gray-400 font-mono whitespace-pre-wrap")
            _refresh_status()

        async def restart_bridge():
            if sys.platform == "win32":
                bridge_log_lbl.text = "Stopping native bridge process..."
                bridge_log_lbl.classes(replace="text-xs text-yellow-400 font-mono whitespace-pre-wrap")
                _pu.kill_matching("mt5_bridge.py")
                await asyncio.sleep(2)
                _pu.kill_matching("mt5_bridge.py", force=True)
                await asyncio.sleep(1)
            else:
                bridge_log_lbl.text = "Stopping Wine session..."
                bridge_log_lbl.classes(replace="text-xs text-yellow-400 font-mono whitespace-pre-wrap")
                # Kill the ENTIRE Wine session — not just the bridge process.
                # Leaving wineserver alive causes mt5.initialize() in the new bridge
                # to spawn a second terminal64.exe window rather than reusing the
                # existing one.
                for pat in ("wineserver", "mt5_bridge.py", "terminal64.exe", "winewrapper"):
                    for pid in _pu.pids_matching(pat):
                        _pu.kill_pid(pid)

                await asyncio.sleep(3)

                for pat in ("wineserver", "mt5_bridge.py", "terminal64.exe", "winewrapper"):
                    for pid in _pu.pids_matching(pat):
                        _pu.kill_pid(pid, force=True)

                await asyncio.sleep(2)
            await start_bridge()

        async def setup_bridge_deps():
            """Download Python 3.11 and install MetaTrader5 inside the Wine bottle."""
            import asyncio

            _backend = backend_sel.value
            if _backend == "crossover":
                _bottle = os.path.expanduser(
                    "~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
                )
                _extra_env = {"CX_BOTTLE": _bottle, "CX_NO_BROWSER": "1"}
            else:
                _bottle = _mt5_bottle_path()
                _extra_env = {}

            if not os.path.isdir(_bottle):
                bridge_log_lbl.text = f"Wine bottle not found:\n{_bottle}"
                bridge_log_lbl.classes(replace="text-xs text-red-400 font-mono whitespace-pre-wrap")
                return

            _wine = _wine_bin()
            env_vars = {
                **os.environ,
                "WINEPREFIX":    _bottle,
                "WINEDEBUG":     "-all",
                **_extra_env,
            }

            lines: list[str] = []

            def _log(text: str):
                lines.append(text)
                bridge_log_lbl.text = "\n".join(lines)
                bridge_log_lbl.classes(replace="text-xs text-gray-300 font-mono whitespace-pre-wrap")

            lines.clear()
            bridge_log_lbl.text = ""

            # ── Step 1: Python 3.11 ───────────────────────────────────────────
            _python_host = os.path.join(_bottle, "drive_c", "Python311", "python.exe")
            if os.path.isfile(_python_host):
                _log("[1/2] Python 3.11 — already installed, skipping.")
            else:
                _py_ver = "3.11.9"
                _py_url = f"https://www.python.org/ftp/python/{_py_ver}/python-{_py_ver}-amd64.exe"
                _installer_mac = f"/tmp/python-{_py_ver}-amd64.exe"
                _log(f"[1/2] Downloading Python {_py_ver} (~25 MB)...")
                try:
                    dl = await asyncio.create_subprocess_exec(
                        "curl", "-fL", "-o", _installer_mac, _py_url,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    dl_out, _ = await dl.communicate()
                    if dl.returncode != 0:
                        _log(f"      Download failed (code {dl.returncode}):")
                        _log(dl_out.decode(errors="replace")[:400])
                        return
                    _log("      Downloaded OK.")
                except Exception as exc:
                    _log(f"      Download error: {exc}")
                    return

                _installer_win = "Z:" + _installer_mac.replace("/", "\\")
                _log("      Installing Python inside Wine (1-3 minutes, please wait)...")
                try:
                    inst = await asyncio.create_subprocess_exec(
                        _wine, _installer_win,
                        "/quiet", "InstallAllUsers=0",
                        "TargetDir=C:\\Python311",
                        "Include_pip=1", "Shortcuts=0", "PrependPath=0",
                        env=env_vars,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    await asyncio.wait_for(inst.communicate(), timeout=300)
                except asyncio.TimeoutError:
                    _log("      Installer timed out (>5 min). Check CrossOver manually.")
                    return
                except Exception as exc:
                    _log(f"      Installer error: {exc}")
                    return

                if os.path.isfile(_python_host):
                    _log("      Python 3.11 installed successfully.")
                else:
                    _log("      Installation appears to have failed — python.exe not found.")
                    _log(f"      Expected: {_python_host}")
                    _log("      Try installing Python 3.11 manually inside CrossOver.")
                    return

            # ── Step 2: MetaTrader5 package ───────────────────────────────────
            _log("[2/2] Installing MetaTrader5 Python package...")
            try:
                pip = await asyncio.create_subprocess_exec(
                    _wine, "C:\\Python311\\python.exe",
                    "-m", "pip", "install", "MetaTrader5",
                    env=env_vars,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                pip_out, _ = await asyncio.wait_for(pip.communicate(), timeout=180)
                pip_text = pip_out.decode(errors="replace")
                if "Successfully installed" in pip_text or "already satisfied" in pip_text.lower():
                    _log("      MetaTrader5 package ready.")
                else:
                    _log("      pip output:")
                    _log(pip_text[:600])
            except asyncio.TimeoutError:
                _log("      pip timed out.")
            except Exception as exc:
                _log(f"      pip error: {exc}")
                return

            _log("\nSetup complete. Click 'Start Bridge' now.")

        with ui.row().classes("gap-2 mb-2 flex-wrap"):
            ui.button("Start Bridge", icon="play_arrow", on_click=start_bridge).classes(
                "bg-green-700 text-white px-4 py-2"
            )
            ui.button("Stop Bridge", icon="stop", on_click=stop_bridge).classes(
                "bg-red-800 text-white px-4 py-2"
            )
            ui.button("Restart Bridge", icon="restart_alt", on_click=restart_bridge).classes(
                "bg-yellow-700 text-white px-4 py-2"
            )
            ui.button("Refresh Status", icon="refresh", on_click=_refresh_status).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
        if sys.platform != "win32":
            with ui.row().classes("mb-2"):
                ui.button(
                    "Setup Bridge Dependencies", icon="build", on_click=setup_bridge_deps
                ).classes("bg-indigo-700 text-white px-4 py-2").tooltip(
                    "Downloads Python 3.11 and installs MetaTrader5 inside the Wine bottle. "
                    "One-time setup needed on a fresh computer."
                )

        bridge_log_lbl
        ui.separator().classes("my-1")
        _bridge_note = (
            "The bridge must be running for MT5 data to appear. "
            "MetaTrader 5 must also be open and logged into the correct account. "
            + ("(Windows: the bridge connects directly — no Wine required.)"
               if sys.platform == "win32" else
               "(macOS: the bridge runs via Wine/CrossOver.)")
        )
        ui.label(_bridge_note).classes("text-xs text-gray-500 italic")


def _render_diagnostics(engine):
    import re as _re
    import time as _time_mod
    from pathlib import Path as _Path

    diag_container    = ui.column().classes("w-full")
    live_diag_card    = ui.card().classes("w-full bg-gray-900 border border-gray-700 rounded-lg p-3 hidden")
    _live_diag_active = [False]
    _live_diag_timer  = [None]

    async def _render_live_lines():
        live_diag_card.clear()
        with live_diag_card:
            with ui.row().classes("items-center justify-between mb-2"):
                ui.label("Live Diagnostics — since last restart").classes(
                    "text-purple-300 font-bold text-sm"
                )
                ui.label("Auto-refreshes every 5 seconds  ·  Errors / Warnings / Events only").classes(
                    "text-gray-500 text-xs"
                )

            # Offloaded to the DB worker thread — this reads and parses the
            # entire log file (can run tens of MB) every 5 seconds; doing that
            # synchronously on the event loop blocked the whole app for as
            # long as this took, same class of bug as the sqlite3.connect()
            # sites fixed earlier.
            lines = await settings_ctl.live_log_lines()
            if not lines:
                ui.label(
                    "No events recorded yet since last restart. Start the engine and generate some activity."
                ).classes("text-gray-500 text-sm font-mono")
                return

            _LEVEL_CLS = {
                "error":   "text-red-400",
                "warning": "text-yellow-300",
                "event":   "text-gray-300",
            }
            with ui.scroll_area().classes("w-full").style("max-height:500px"):
                with ui.column().classes("w-full gap-0"):
                    for level, ln in lines:
                        cls = _LEVEL_CLS.get(level, "text-gray-400")
                        ui.label(ln).classes(
                            f"font-mono text-xs {cls} leading-relaxed whitespace-pre-wrap break-all"
                        )

    def _toggle_live_diag():
        _live_diag_active[0] = not _live_diag_active[0]
        if _live_diag_active[0]:
            live_diag_card.classes(remove="hidden")
            asyncio.create_task(_render_live_lines())
            if _live_diag_timer[0] is None:
                from nicegui import ui as _ui
                _live_diag_timer[0] = _ui.timer(5.0, _render_live_lines)
        else:
            live_diag_card.classes(add="hidden")
            if _live_diag_timer[0]:
                _live_diag_timer[0].cancel()
                _live_diag_timer[0] = None

    async def run_diag():
        import platform as _platform
        import sys as _sys
        import time as _t

        diag_container.clear()
        with diag_container:
            ui.spinner()

        # ── Timed bridge calls ─────────────────────────────────────────────
        t0 = _t.monotonic()
        try:
            health  = await engine.get_bridge_health()
        except Exception as e:
            diag_container.clear()
            with diag_container:
                ui.label(f"Diagnostics error: {e}").classes("text-red-400")
            return
        health_ms = round((_t.monotonic() - t0) * 1000)

        t1 = _t.monotonic()
        try:
            tick    = await engine.get_tick()
        except Exception:
            tick = None
        tick_ms = round((_t.monotonic() - t1) * 1000)

        t2 = _t.monotonic()
        try:
            mt5_acc = await engine.get_mt5_account()
        except Exception:
            mt5_acc = None
        acc_ms = round((_t.monotonic() - t2) * 1000)

        # ── Telegram → execution latency from DB ───────────────────────────
        tg_latency: dict = {}
        try:
            from backend.src.config import DATA_DIR as _ddir, load as _cfg_load
            _env = _cfg_load().get("account_env", "demo")
            _db_path = str(_ddir / f"forex_trader_{_env}.db")
            try:
                # parsed_at = when signal was received by the app
                # open_time = when the trade was actually opened in MT5
                _rows = settings_ctl.fetch_signal_execution_lags(_db_path)
                lags = [float(r["lag_s"]) for r in _rows if r["lag_s"] is not None]
                if lags:
                    lags_sorted = sorted(lags)
                    n = len(lags_sorted)
                    tg_latency = {
                        "count":  n,
                        "min_s":  round(lags_sorted[0], 2),
                        "avg_s":  round(sum(lags) / n, 2),
                        "p90_s":  round(lags_sorted[int(n * 0.9)], 2),
                        "max_s":  round(lags_sorted[-1], 2),
                    }
            finally:
                pass
        except Exception:
            pass

        diag_container.clear()
        with diag_container:
            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
                ui.label("MT5 Bridge").classes("font-bold text-yellow-300 mb-2")
                bridge_ok     = health.get("connected", False)
                trade_allowed = health.get("trade_allowed")
                with ui.row().classes("gap-2 flex-wrap items-center"):
                    ui.badge(
                        "Connected" if bridge_ok else "Disconnected",
                        color="green" if bridge_ok else "red",
                    )
                    if bridge_ok:
                        if trade_allowed is False:
                            ui.badge("AutoTrading OFF", color="orange")
                            ui.label(
                                "AutoTrading is disabled in MetaTrader 5. "
                                "Click the AutoTrading button (robot icon) in the MT5 toolbar "
                                "to allow the bridge to place orders."
                            ).classes("text-orange-300 text-xs mt-1 w-full")
                        elif trade_allowed is True:
                            ui.badge("AutoTrading ON", color="green")

                    from backend.src.services.broker import ea_bridge as _ea_bridge_mod
                    _ea_ok, _ea_scope = _ea_bridge_mod.get_effective_ea_status()
                    ui.badge(
                        f"EA {'Connected' if _ea_ok else 'Not Connected'}"
                        + ("" if _ea_scope == "this node" else f" ({_ea_scope})"),
                        color="green" if _ea_ok else "red",
                    )
                if health.get("last_error"):
                    ui.label(f"Last error: {health['last_error']}").classes("text-red-300 text-sm mt-1")
                if not _ea_ok:
                    if _ea_scope == "this node":
                        _ea = _ea_bridge_mod.get_instance()
                        _ea_hint = (
                            "No MQL5 EA has ever connected this session."
                            if _ea is None or _ea._last_seen == 0 else
                            f"EA connection lost {round(_time_mod.time() - _ea._last_seen)}s ago."
                        )
                    else:
                        _ea_hint = f"The {_ea_scope} (active trader) has no EA connected."
                    ui.label(
                        f"{_ea_hint} Trades will still be managed by the Python bridge -- "
                        "this only affects EA-native on-tick management. If unexpected, check "
                        "that ForexTraderBridge is attached to the chart with AutoTrading on."
                    ).classes("text-gray-400 text-xs mt-1 w-full")

            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("Market Data").classes("font-bold text-yellow-300 mb-2")
                if tick:
                    ui.label(
                        f"Bid: {tick.bid}  Ask: {tick.ask}  Spread: {tick.spread_points:.0f}pt"
                    ).classes("text-green-300 font-mono text-sm")
                    ui.label(f"Source: {tick.source}").classes("text-gray-400 text-xs")
                else:
                    ui.label("No tick data").classes("text-red-400")

            if mt5_acc:
                with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                    ui.label("MT5 Account").classes("font-bold text-yellow-300 mb-2")
                    ui.label(
                        f"Balance:  ${float(mt5_acc.get('balance', 0)):,.2f}"
                    ).classes("text-gray-200 font-mono text-sm")
                    ui.label(
                        f"Equity:   ${float(mt5_acc.get('equity', 0)):,.2f}"
                    ).classes("text-gray-200 font-mono text-sm")
                    ui.label(f"Server:   {mt5_acc.get('server', '?')}").classes(
                        "text-gray-400 text-xs"
                    )
                    ui.label(f"Is demo:  {mt5_acc.get('is_demo', '?')}").classes(
                        "text-gray-400 text-xs"
                    )

            # MT5 history import
            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("MT5 History Sync").classes("font-bold text-yellow-300 mb-2")
                ui.label(
                    "Pull closed positions from MT5 bridge and import them into the local database. "
                    "Skips positions already recorded."
                ).classes("text-xs text-gray-400 mb-2")
                sync_result = ui.label("").classes("text-sm text-gray-300")
                days_inp = ui.number("Days to look back", value=90, min=1, max=365, step=1).classes(
                    "w-40"
                )

                async def do_sync():
                    sync_result.text = "Syncing..."
                    try:
                        result = await engine.import_mt5_history(int(days_inp.value))
                        sync_result.text = (
                            f"Done — imported {result['imported']}, "
                            f"skipped {result['skipped']}."
                            + (f" {result.get('error', '')}" if result.get("error") else "")
                        )
                        ui.notify(
                            f"Sync complete: {result['imported']} imported",
                            type="positive",
                        )
                    except Exception as e:
                        sync_result.text = f"Error: {e}"
                        ui.notify(str(e), type="negative")

                ui.button("Import MT5 History", on_click=do_sync).classes(
                    "bg-blue-700 text-white px-4 py-2 mt-2"
                )
                sync_result

            # ── Latency Report ───────────────────────────────────────────────
            _is_mac  = _sys.platform == "darwin"
            _is_wine = _is_mac  # MT5 runs via Wine/CrossOver on Mac
            _plat_lbl = (
                f"macOS {_platform.mac_ver()[0]} — MT5 via Wine/CrossOver"
                if _is_mac else
                f"Windows {_platform.version()}"
            )
            _bridge_url = "localhost:9000"

            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("Latency Report").classes("font-bold text-yellow-300 mb-2")
                ui.label(_plat_lbl).classes("text-xs text-gray-500 mb-3 italic")

                # ── Path 1: App → Bridge → MT5 → Vantage ────────────────────
                ui.label("Path 1 — App → Bridge → MT5 → Vantage").classes(
                    "text-xs font-semibold text-blue-300 uppercase mb-1"
                )
                with ui.element("div").style(
                    "display:grid; grid-template-columns:repeat(3,1fr); gap:8px;"
                ):
                    _steps_1 = [
                        ("App → Bridge\n(HTTP /health)", f"{health_ms} ms",
                         "text-green-400" if health_ms < 50 else "text-yellow-300" if health_ms < 200 else "text-red-400"),
                        ("App → Bridge\n(HTTP /tick)", f"{tick_ms} ms",
                         "text-green-400" if tick_ms < 50 else "text-yellow-300" if tick_ms < 200 else "text-red-400"),
                        ("App → Bridge\n(HTTP /account)", f"{acc_ms} ms",
                         "text-green-400" if acc_ms < 50 else "text-yellow-300" if acc_ms < 200 else "text-red-400"),
                    ]
                    for lbl, val, cls in _steps_1:
                        with ui.card().classes("bg-gray-900 rounded p-2 text-center"):
                            ui.label(val).classes(f"text-sm font-bold {cls}")
                            ui.label(lbl).classes("text-xs text-gray-500 whitespace-pre-line")

                _total_bridge = health_ms
                if _is_wine:
                    _wine_est = "~5–20 ms (Wine IPC overhead)"
                    ui.label(
                        f"Wine/CrossOver adds IPC overhead between the bridge and MT5 "
                        f"({_wine_est}). Total app→MT5 round-trip is approximately "
                        f"{_total_bridge + 15} ms under typical conditions."
                    ).classes("text-xs text-gray-400 italic mt-1 mb-3")
                else:
                    ui.label(
                        f"Total app→bridge round-trip: {_total_bridge} ms (direct Windows, no Wine)."
                    ).classes("text-xs text-gray-400 italic mt-1 mb-3")

                # ── Path 2: Telegram → App → Bridge → MT5 → Vantage ─────────
                ui.label("Path 2 — Telegram signal → Trade execution").classes(
                    "text-xs font-semibold text-purple-300 uppercase mb-1"
                )
                if tg_latency:
                    with ui.element("div").style(
                        "display:grid; grid-template-columns:repeat(5,1fr); gap:8px;"
                    ):
                        for lbl, val_s, hint_cls in [
                            ("Fastest\nexecution", f"{tg_latency['min_s']}s", "text-green-400"),
                            ("Average\n(end-to-end)", f"{tg_latency['avg_s']}s", "text-yellow-300"),
                            ("P90\nlatency", f"{tg_latency['p90_s']}s", "text-orange-300"),
                            ("Slowest\nrecorded", f"{tg_latency['max_s']}s", "text-red-400"),
                            ("Sample\nsize", str(tg_latency['count']), "text-gray-300"),
                        ]:
                            with ui.card().classes("bg-gray-900 rounded p-2 text-center"):
                                ui.label(val_s).classes(f"text-sm font-bold {hint_cls}")
                                ui.label(lbl).classes("text-xs text-gray-500 whitespace-pre-line")

                    ui.label(
                        "Measured from Telegram signal parsed_at to MT5 open_time. "
                        "Includes: Telegram poll lag → signal parse → app processing → "
                        f"HTTP bridge call ({health_ms} ms typical)"
                        + (" → Wine/CrossOver IPC → MT5 → Vantage order routing." if _is_wine
                           else " → MT5 → Vantage order routing.")
                    ).classes("text-xs text-gray-400 italic mt-2")
                    if _is_wine:
                        ui.label(
                            "On macOS with CrossOver, Wine IPC adds latency between the Python bridge "
                            "and MetaTrader 5. To minimise this, keep CrossOver and MT5 running "
                            "continuously and avoid cold starts."
                        ).classes("text-xs text-orange-300 italic mt-1")
                else:
                    ui.label(
                        "No Telegram signal → trade execution pairs found in the database. "
                        "Latency will be calculated once live signals are received and traded."
                    ).classes("text-xs text-gray-500 italic")

    export_lbl = ui.label("").classes("text-sm mt-1")

    async def export_logs():
        import base64 as _b64
        import platform as _platform
        import sys as _sys
        import time as _time
        from datetime import datetime as _dt
        from pathlib import Path as _Path
        from backend.src.controllers import settings_controller as _db_ctl
        import httpx as _httpx

        DAYS       = 5
        MAX_BYTES  = 25 * 1024 * 1024   # 25 MB raw → ~33 MB base64 → well under Resend 40 MB cap
        TO = "simon.moore@outlook.com"

        # ── Polling endpoints that produce only noise ─────────────────────────
        # These are httpx GET lines that fire every few seconds continuously.
        _POLL_DROPS = (
            "/tick/XAUUSD",
            "/candles/XAUUSD",
            "/candles_symbol/",
            "localhost:9000/positions ",
            "localhost:9000/account ",
            "localhost:9000/health",
            "/history?days=",
            "/history/position/",
            "getUpdates",
        )

        def _keep_line(ln: str) -> bool:
            """Return True only for log lines worth including in a troubleshooting export."""
            # Drop all DEBUG lines
            if " DEBUG " in ln:
                return False
            # Drop httpx GET polling lines — these are background heartbeats
            if "INFO httpx" in ln and "HTTP Request: GET" in ln:
                for _drop in _POLL_DROPS:
                    if _drop in ln:
                        return False
            return True

        export_lbl.text = "Reading and filtering logs..."
        export_lbl.classes(replace="text-sm mt-1 text-gray-400")

        try:
            # ── Collect log files (current + rotated backups) ─────────────────
            from backend.src.config import DATA_DIR as _log_data_dir
            log_base = _Path(_log_data_dir) / "forex_trader.log"
            if not log_base.exists():
                export_lbl.text = "No log file found — restart the app first to begin writing logs."
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            # TimedRotatingFileHandler names backups forex_trader.log.YYYY-MM-DD.
            # Ascending sort gives chronological order (oldest date first),
            # then append the current (today's) file at the end.
            candidates = sorted(
                log_base.parent.glob("forex_trader.log.2*"),
            ) + [log_base]

            cutoff    = _time.time() - DAYS * 86400
            raw_total = 0       # total lines seen (before filter)
            lines     = []      # filtered lines to include

            for path in candidates:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            stripped = line.rstrip()
                            if not stripped:
                                continue
                            raw_total += 1
                            # Date-range filter
                            try:
                                ts = _dt.strptime(stripped[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
                                if ts < cutoff:
                                    continue
                            except Exception:
                                pass  # non-timestamped lines (tracebacks etc) — keep
                            if _keep_line(stripped):
                                lines.append(stripped)
                except Exception:
                    pass  # skip unreadable rotation files

            if not lines:
                export_lbl.text = (
                    f"No meaningful log entries in the last {DAYS} days "
                    f"({raw_total:,} lines scanned — all were routine polling)."
                )
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            # ── Deduplicate consecutive repeated lines ────────────────────────
            # httpx POST /modify fires every 5 s while a trade is live — collapse those.
            deduped: list[str] = []
            prev_body  = None
            repeat_cnt = 0
            for ln in lines:
                body = ln[24:] if len(ln) > 24 else ln   # strip timestamp
                if body == prev_body:
                    repeat_cnt += 1
                else:
                    if repeat_cnt > 0:
                        deduped.append(f"        ... repeated {repeat_cnt} more time(s)")
                    deduped.append(ln)
                    prev_body  = body
                    repeat_cnt = 0
            if repeat_cnt > 0:
                deduped.append(f"        ... repeated {repeat_cnt} more time(s)")

            filtered_total = len(deduped)
            now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
            fname   = f"forex_trader_logs_{_dt.now().strftime('%Y%m%d_%H%M')}.txt"

            # ── Gather user and system info ────────────────────────────────────
            try:
                from backend.src.config.licence import store as _lic_store
                from backend.src.config.licence.fingerprint import get_fingerprint as _fp
                _lic_data    = _lic_store.load() or {}
                _lic_email   = _lic_data.get("email", "—")
                _lic_type    = _lic_data.get("licence_type", "—")
                _lic_expiry  = _lic_data.get("expiry_date", "—")
                _machine_id  = _fp()
            except Exception:
                _lic_email = _lic_type = _lic_expiry = _machine_id = "—"

            try:
                from backend.src.utils.version_history import __version__ as _app_ver
            except Exception:
                _app_ver = "—"

            _env_label = (
                settings_ctl.get_app_config("account_env") or "demo"
            ).upper()
            _py_ver  = _sys.version.split()[0]
            _os_info = f"{_platform.system()} {_platform.release()} ({_platform.machine()})"

            _sys_header = (
                f"FOREX Trader — Filtered Log Export\n"
                f"{'=' * 80}\n"
                f"Generated  : {now_str}\n"
                f"Period     : last {DAYS} days\n"
                f"Raw lines  : {raw_total:,}  (scanned)\n"
                f"Kept lines : {filtered_total:,}  (errors, warnings, app events)\n"
                f"Dropped    : {raw_total - filtered_total:,}  (DEBUG + polling noise)\n"
                f"\n"
                f"-- User Info --\n"
                f"Email      : {_lic_email}\n"
                f"Machine ID : {_machine_id}\n"
                f"Licence    : {_lic_type}  (expires: {_lic_expiry})\n"
                f"\n"
                f"-- System Info --\n"
                f"App version: {_app_ver}\n"
                f"Environment: {_env_label}\n"
                f"Python     : {_py_ver}\n"
                f"OS         : {_os_info}\n"
                f"{'=' * 80}\n\n"
            )

            # ── Build attachment with hard 25 MB cap ──────────────────────────
            body_text = "\n".join(deduped)
            body_enc  = body_text.encode("utf-8")
            header_enc = _sys_header.encode("utf-8")
            truncated  = False

            if len(header_enc) + len(body_enc) > MAX_BYTES:
                # Keep the most recent lines that fit (newest are at the end)
                available = MAX_BYTES - len(header_enc) - 200  # 200 b margin
                tail_bytes = body_enc[-available:]
                # Realign to the next newline boundary so we don't split a line
                nl_idx = tail_bytes.find(b"\n")
                if nl_idx != -1:
                    tail_bytes = tail_bytes[nl_idx + 1:]
                body_text = (
                    "[OLDEST ENTRIES TRUNCATED — file exceeded 25 MB limit]\n\n"
                    + tail_bytes.decode("utf-8", errors="replace")
                )
                truncated = True

            attachment_bytes = (header_enc + body_text.encode("utf-8"))
            attachment_b64   = _b64.b64encode(attachment_bytes).decode()

            # ── HTML email body ────────────────────────────────────────────────
            _trunc_note = (
                "<p style='color:#f87171;font-size:13px;margin-top:8px;'>"
                "Note: attachment was truncated to fit the 25 MB limit — oldest entries removed."
                "</p>"
            ) if truncated else ""
            size_kb = round(len(attachment_bytes) / 1024)
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#111827;font-family:Arial,sans-serif;color:#e5e7eb;">
<h2 style="color:#f59e0b;margin-bottom:6px;">FOREX Trader &mdash; Filtered Log Export</h2>
<p style="color:#9ca3af;font-size:13px;">
  {now_str} &nbsp;&bull;&nbsp; v{_app_ver}
  &nbsp;&bull;&nbsp; Last {DAYS} days
  &nbsp;&bull;&nbsp; {filtered_total:,} kept / {raw_total:,} scanned
  &nbsp;&bull;&nbsp; {size_kb} KB
</p>
{_trunc_note}
<table style="border-collapse:collapse;margin-top:16px;font-size:13px;">
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Email</td>
      <td style="color:#e5e7eb">{_lic_email}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Machine ID</td>
      <td style="color:#e5e7eb;font-family:monospace">{_machine_id}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Licence</td>
      <td style="color:#e5e7eb">{_lic_type} &mdash; expires {_lic_expiry}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Environment</td>
      <td style="color:#e5e7eb">{_env_label}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">App version</td>
      <td style="color:#e5e7eb">{_app_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Python</td>
      <td style="color:#e5e7eb">{_py_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">OS</td>
      <td style="color:#e5e7eb">{_os_info}</td></tr>
</table>
<p style="color:#d1d5db;font-size:13px;margin-top:16px;">
  Filtered log attached as <strong>{fname}</strong>.<br>
  <span style="color:#6b7280;font-size:12px;">
    Dropped: DEBUG lines, tick polling, candle polling, Telegram getUpdates, position checks.
    Kept: errors, warnings, trades, signals, bridge events, DPM actions, auth events.
  </span>
</p>
</body></html>"""

            # ── Send via Resend with attachment ───────────────────────────────
            ecfg    = _db_ctl.get_email_config()
            api_key = (ecfg.get("resend_api_key") or "").strip()
            if not api_key:
                export_lbl.text = "No Resend API key configured — set it in Settings → Email Reports."
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            payload = {
                "from":    "FOREX Trader <onboarding@resend.dev>",
                "to":      [TO],
                "subject": f"FOREX Trader Logs — {now_str}",
                "html":    html,
                "attachments": [
                    {"filename": fname, "content": attachment_b64},
                ],
            }

            export_lbl.text = (
                f"Sending {filtered_total:,} filtered lines "
                f"({size_kb} KB) to {TO}..."
            )
            async with _httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code in (200, 201):
                export_lbl.text = (
                    f"Sent — {filtered_total:,} kept / {raw_total:,} scanned "
                    f"({size_kb} KB)."
                    + (" [truncated]" if truncated else "")
                )
                export_lbl.classes(replace="text-sm mt-1 text-green-400")
                ui.notify("Log export sent!", type="positive")
            else:
                export_lbl.text = f"Send failed (HTTP {r.status_code}): {r.text[:200]}"
                export_lbl.classes(replace="text-sm mt-1 text-red-400")

        except Exception as exc:
            export_lbl.text = f"Error: {exc}"
            export_lbl.classes(replace="text-sm mt-1 text-red-400")

    # ── Prevent-sleep toggle ────────────────────────────────────────────────
    global _caffeinate_proc

    def _sleep_is_active() -> bool:
        return _pu.is_preventing_sleep(_caffeinate_proc)

    def _update_sleep_btn(btn):
        if _sleep_is_active():
            btn.text = "Prevent Sleep: ON"
            btn.props("color=green")
        else:
            btn.text = "Prevent Sleep: OFF"
            btn.props("color=grey")

    def _toggle_sleep(btn):
        global _caffeinate_proc
        if _sleep_is_active():
            _pu.stop_prevent_sleep(_caffeinate_proc)
            _caffeinate_proc = None
            ui.notify("Sleep prevention disabled", type="info", position="top")
        else:
            try:
                _caffeinate_proc = _pu.start_prevent_sleep()
                msg = (
                    "Sleep prevention enabled — Windows will stay awake"
                    if sys.platform == "win32"
                    else "Sleep prevention enabled — Mac will stay awake"
                )
                ui.notify(msg, type="positive", position="top")
            except Exception as exc:
                ui.notify(f"Failed to enable sleep prevention: {exc}", type="negative", position="top")
        _update_sleep_btn(btn)

    _os_label = "Windows" if sys.platform == "win32" else "Mac"
    sleep_btn = ui.button(
        "Prevent Sleep: OFF",
        icon="bedtime_off",
        on_click=lambda: _toggle_sleep(sleep_btn),
    ).classes("px-4 py-2").tooltip(
        f"Keep the {_os_label} awake so no trade is missed. "
        "The screen can still turn off. "
        "Automatically releases when the app exits."
    )
    _update_sleep_btn(sleep_btn)

    # ── Auto-restart watchdog toggle ────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("w-full items-center justify-between"):
            auto_restart_sw = ui.switch(
                "Auto-Restart if the app stops",
                value=(db_module.get_app_config("auto_restart_enabled") == "1"),
            ).classes("text-blue-300 font-bold")
            ui.icon("restart_alt", size="sm").classes("text-blue-400")

        _autostart_lbl = ui.label("").classes("text-xs mt-1 text-gray-500")

        with ui.expansion(
            "How does Auto-Restart work?", icon="info_outline"
        ).classes("w-full text-sm"):
            ui.markdown(
                f"When **ON**, {_os_label} checks every "
                f"{_autostart.CHECK_INTERVAL_SECS // 60} minutes that the app is "
                "still serving on its port, and starts it again if it is not. "
                "It also runs that check at login/boot, so the app comes back by "
                "itself after a reboot.\n\n"
                + (
                    "Registered as a **LaunchAgent** (`launchctl`) under your user "
                    "account.\n\n"
                    if sys.platform == "darwin"
                    else "Registered as a **Scheduled Task** under your user account.\n\n"
                )
                + "Stopping the app deliberately still stops it — "
                "`FOREX Stop.command` / `Stop FOREX.bat` pause the watchdog, and "
                "starting the app again re-arms it. Restarting from inside the app "
                "is unaffected.\n\n"
                "When **OFF**, nothing restarts the app and it stays down until "
                "started by hand."
            ).classes("text-gray-300")

        def _refresh_autostart_lbl():
            if not _autostart.is_supported():
                _autostart_lbl.text = f"Not supported on this platform ({sys.platform})."
                _autostart_lbl.classes(replace="text-xs mt-1 text-gray-500")
                return
            if not auto_restart_sw.value:
                _autostart_lbl.text = "Off — nothing will restart the app if it stops."
                _autostart_lbl.classes(replace="text-xs mt-1 text-gray-500")
            elif _autostart.is_installed():
                _autostart_lbl.text = (
                    f"Active — checking every "
                    f"{_autostart.CHECK_INTERVAL_SECS // 60} min."
                    + ("" if _autostart.is_armed() else " Currently paused (app stopped).")
                )
                _autostart_lbl.classes(replace="text-xs mt-1 text-green-400")
            else:
                _autostart_lbl.text = "On, but the scheduler entry is missing — toggle off and on to repair."
                _autostart_lbl.classes(replace="text-xs mt-1 text-yellow-400")

        # Reverting the switch after a failure re-fires this handler, which
        # would run disable() a second time and stack a second notification on
        # top of the error the user actually needs to read.
        _autostart_reverting = {"active": False}

        def _revert_switch(to_value: bool):
            _autostart_reverting["active"] = True
            try:
                auto_restart_sw.value = to_value
            finally:
                _autostart_reverting["active"] = False
            _refresh_autostart_lbl()

        def _on_autostart_change(e):
            if _autostart_reverting["active"]:
                return
            want = bool(e.value)
            if want and not _autostart.is_supported():
                ui.notify(
                    f"Auto-restart is not supported on {sys.platform}",
                    type="warning", position="top",
                )
                _revert_switch(False)
                return
            try:
                if want:
                    _autostart.enable()
                else:
                    _autostart.disable()
            except Exception as exc:
                # Leave the stored setting alone when the OS refused — a toggle
                # showing ON with no scheduler entry behind it is exactly the
                # false sense of safety this feature exists to remove.
                ui.notify(f"Could not enable auto-restart: {exc}",
                          type="negative", position="top")
                _revert_switch(not want)
                return
            db_module.set_app_config("auto_restart_enabled", "1" if want else "0")
            ui.notify(
                "Auto-restart enabled" if want else "Auto-restart disabled",
                type="positive" if want else "info", position="top",
            )
            _refresh_autostart_lbl()

        auto_restart_sw.on_value_change(_on_autostart_change)
        _refresh_autostart_lbl()

    with ui.row().classes("gap-2 mb-2 items-center flex-wrap"):
        ui.button("Run Diagnostics", on_click=run_diag).classes(
            "bg-blue-700 text-white px-4 py-2"
        )
        ui.button("Live Diagnostics", icon="terminal", on_click=_toggle_live_diag).classes(
            "bg-purple-700 text-white px-4 py-2"
        ).tooltip("Stream live app logs (errors, warnings and events) since last restart")
        ui.button("Export Logs", icon="mail", on_click=export_logs).classes(
            "bg-gray-700 text-white px-4 py-2"
        ).tooltip("Email the last 5 days of logs as a .txt attachment to the configured report address")
    export_lbl

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-1 mb-2"):
            ui.label("Data Retention").classes("font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Applies to this node's own database: Telegram messages, closed "
                "trades (open trades are never touched), and resolved signal/"
                "parse history (vantage_signals, vantage_tg_signals, unrecognised "
                "messages, AI-recovered signals). Checked once a day. "
                "Indefinite (default) never deletes anything — this matches how "
                "the app has always behaved; there was no retention limit before "
                "this setting existed."
            )
        ui.label(
            "How long historical data is kept before being automatically deleted."
        ).classes("text-xs text-gray-500 mb-3")

        _retention_options = {
            "0": "Indefinite (never delete)",
            "30": "30 days",
            "90": "90 days",
            "180": "180 days",
            "365": "365 days",
            "custom": "Custom…",
        }
        _current_days = settings_ctl.get_data_retention_days()
        _preset_days = {0, 30, 90, 180, 365}
        _initial_key = str(_current_days) if _current_days in _preset_days else "custom"

        with ui.row().classes("items-center gap-2 flex-wrap"):
            retention_select = ui.select(_retention_options, value=_initial_key).classes("w-56")
            custom_days_input = ui.number(
                value=_current_days if _initial_key == "custom" else 0,
                min=1, step=1, format="%.0f", placeholder="days",
            ).classes("w-24")
            custom_days_input.visible = _initial_key == "custom"
            save_retention_btn = ui.button("Save", icon="save").classes(
                "bg-blue-700 text-white"
            )

        def _on_retention_select_change(e):
            custom_days_input.visible = (e.value == "custom")

        retention_select.on_value_change(_on_retention_select_change)

        def _save_retention():
            key = retention_select.value
            if key == "custom":
                days = int(custom_days_input.value or 0)
                if days <= 0:
                    ui.notify(
                        "Enter a number of days greater than 0, or choose Indefinite.",
                        type="warning",
                    )
                    return
            else:
                days = int(key)
            settings_ctl.set_data_retention_days(days)
            ui.notify(
                "Data retention set to indefinite — nothing will be deleted."
                if days == 0 else
                f"Data retention set to {days} days — older Telegram messages, "
                f"closed trades, and signal history will be pruned daily.",
                type="positive",
            )

        save_retention_btn.on_click(_save_retention)

    live_diag_card
    diag_container

    return run_diag


# ── Registration tab ───────────────────────────────────────────────────────────

def _render_theme():
    """Settings → Theme tab — pick Light or Dark for the whole app.

    See core_ui_theme.py's module docstring for how the neutral-class CSS
    override mechanism works (and why text-white is excluded on buttons).
    """
    from frontend import theme as theme_mod

    with ui.column().classes("w-full max-w-2xl gap-3"):
        ui.label("Color Theme").classes("text-base font-bold text-yellow-300")
        ui.label(
            "Applies to the whole app immediately — no restart needed. "
            "Status colors (profit/loss/warnings) never change."
        ).classes("text-xs text-gray-400 mb-2")

        current = {"value": theme_mod.get_theme()}
        cards: dict[str, object] = {}

        def _select(name: str) -> None:
            theme_mod.set_theme(name)
            current["value"] = name
            for key, card in cards.items():
                card.classes(
                    remove="border-blue-500" if key != name else "",
                    add="border-blue-500" if key == name else "border-gray-700",
                )
            ui.run_javascript(
                f'document.documentElement.setAttribute("data-fx-theme","{name}")'
            )
            ui.notify(f"Theme set to {theme_mod.THEME_LABELS[name]}", type="positive")

        with ui.row().classes("gap-3 flex-wrap"):
            for name in theme_mod.THEMES:
                bg, border, text = theme_mod.THEME_SWATCHES[name]
                is_active = name == current["value"]
                card = ui.card().classes(
                    "cursor-pointer p-3 w-48 border-2 "
                    + ("border-blue-500" if is_active else "border-gray-700")
                ).style(f"background:{bg};")
                cards[name] = card
                with card:
                    card.on("click", lambda _e, n=name: _select(n))
                    ui.label(theme_mod.THEME_LABELS[name]).style(
                        f"color:{text};font-weight:600;font-size:13px;"
                    )
                    ui.label(theme_mod.THEME_DESCRIPTIONS[name]).style(
                        f"color:{text};font-size:11px;opacity:0.8;"
                    )
                    with ui.row().classes("gap-1 mt-2"):
                        for swatch_color in (bg, border, text):
                            ui.element("div").style(
                                f"width:16px;height:16px;border-radius:3px;"
                                f"background:{swatch_color};border:1px solid #00000040;"
                            )


def _render_registration():
    """Settings → Registration tab — shows stored licence / account details."""
    from backend.src.config.licence import store as _store
    from backend.src.config.licence.fingerprint import get_fingerprint

    data       = _store.load()
    email      = data.get("email", "") if data else ""
    raw_key    = data.get("licence_key", "") if data else ""
    machine_id = get_fingerprint()

    # Masked key: show first group then stars for the rest
    if raw_key:
        first_group = raw_key.split("-")[0] if "-" in raw_key else raw_key[:8]
        masked_key  = first_group + " - **** - **** - **** - **** - **** - **** - ****"
    else:
        masked_key = "—"

    from datetime import date as _date

    def _reg_days_remaining(expiry_date: str) -> tuple[str, str]:
        if not expiry_date or expiry_date == "perpetual":
            return "Unlimited", "text-green-400"
        try:
            exp   = _date.fromisoformat(expiry_date)
            delta = (exp - _date.today()).days
            if delta < 0:
                return "Expired", "text-red-400"
            if delta == 0:
                return "Expires today", "text-orange-400"
            colour = "text-orange-400" if delta <= 30 else "text-green-400"
            return f"{delta} days", colour
        except Exception:
            return "—", "text-gray-400"

    expiry_date  = data.get("expiry_date", "perpetual") if data else "perpetual"
    licence_type = (data.get("licence_type") or (
        "Perpetual" if expiry_date == "perpetual" else "Fixed Term"
    )) if data else "—"
    days_txt, days_cls = _reg_days_remaining(expiry_date)

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Registration").classes("text-lg font-bold text-yellow-300 mb-4")

        with ui.grid(columns=2).classes("gap-x-8 gap-y-3 text-sm w-full"):
            ui.label("Email").classes("text-gray-400 self-center")
            ui.label(email or "—").classes("text-white font-mono")

            ui.label("Registration Key").classes("text-gray-400 self-center")
            ui.label(masked_key).classes("text-gray-300 font-mono text-xs break-all")

            ui.label("Machine ID").classes("text-gray-400 self-center")
            ui.label(machine_id).classes("text-gray-300 font-mono text-xs break-all")

            ui.label("Licence Type").classes("text-gray-400 self-center")
            ui.label(licence_type).classes("text-gray-300")

            ui.label("Remaining Days").classes("text-gray-400 self-center")
            ui.label(days_txt).classes(days_cls)

