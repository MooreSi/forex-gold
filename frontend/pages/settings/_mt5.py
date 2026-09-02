"""MetaTrader 5 tab: broker credentials, the account environment switch and
the EA update/restart button."""
import asyncio
import os
import subprocess
import sys

from nicegui import app, ui

from backend.src.controllers import settings_controller as settings_ctl

from ._bridge import _render_bridge_control
from ._shared import cfg_module

import logging

_log = logging.getLogger(__name__)
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

    cfg = cfg_module.load_config()
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
                        current_env = cfg_module.get_config("account_env", "demo")
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
                "EA Bridge",
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
        from backend.src.controllers.settings_controller import USER_DATA_DIR
        _result_log = USER_DATA_DIR / "data" / "ea_update_result.log"
        if _result_log.exists():
            _lines = _result_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if _lines:
                ui.label(f"Last update: {_lines[-1]}").classes("text-xs text-gray-500")
    except Exception as e:
        _log.debug("[settings] reading the EA update log failed: %s", e)

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

        # Marker-based: four parents was right from forex_trader/ui/pages/;
        # this file is three deep, so it resolved above the repo and the EA
        # source was never found. (2026-08-26.)
        from backend.src.controllers.system_controller import repo_root as _repo_root
        root = _repo_root()
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
        from backend.src.controllers.settings_controller import USER_DATA_DIR as _ea_user_data_dir
        script = _EA_RESTART_PS1_TEMPLATE.format(
            terminal_id=terminal_id, data_folder=_ea_user_data_dir.name,
        )
        script_path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "ea_update_restart.ps1"
        script_path.write_text(script, encoding="utf-8")

        from backend.src.controllers.system_controller import open_restart_log
        from backend.src.controllers.settings_controller import USER_DATA_DIR
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
