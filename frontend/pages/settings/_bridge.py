"""MT5 bridge control: starting, stopping and reporting on the Wine-hosted
bridge process that stands in for MetaTrader5 off Windows."""
import asyncio
import sys

from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl

from ._shared import _pu, cfg_module

_bridge_proc     = [None]   # holds the Popen for the bridge subprocess
_bridge_starting = [False]  # True while start_bridge() is in the startup window


def _wine_bin() -> str:
    """Wine binary — reads from config (wine_bin key).
    Defaults to CrossOver's binary which ships on this machine; update config.yaml
    to /opt/homebrew/bin/wine after installing a system Wine via Homebrew."""
    from backend.src.controllers import settings_controller as _cfg
    return (_cfg.get_config("wine_bin") or "").strip() or \
        "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"


def _mt5_bottle_path() -> str:
    """Wine prefix (WINEPREFIX) for MT5.
    After running setup_wine_bridge.sh this is ~/.wine_mt5 — an independent
    prefix that does not require CrossOver to be installed."""
    import os as _os
    from backend.src.controllers import settings_controller as _cfg
    return (_cfg.get_config("mt5_bottle_path") or "").strip() or \
        _os.path.expanduser("~/.wine_mt5")


def _bridge_running() -> bool:
    return _pu.is_port_listening(9000)


def _render_bridge_control(engine):
    import subprocess, os

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mb-4"):
        ui.label("MT5 Bridge Control").classes("font-bold text-yellow-300 mb-3")

        _cfg_now = cfg_module.load_config()

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
                    cfg_module.save_config({"bridge_backend": backend_sel.value})
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
            cfg_module.save_config({"mt5_bridge_url": bridge_url_inp.value.strip()})
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

            env = cfg_module.get_config("account_env", "demo")
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
                    from backend.src.controllers.settings_controller import USER_DATA_DIR
                    from urllib.parse import urlparse as _urlparse
                    _creds_path  = str(USER_DATA_DIR / "bridge_credentials.json")
                    _bridge_port = _urlparse(cfg_module.get_config("mt5_bridge_url", "")).port or 9010
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
                from backend.src.controllers import settings_controller as _cfg_mod
                _mac_creds = str(_cfg_mod.USER_DATA_DIR / "bridge_credentials.json")
                _win_creds = "Z:" + _mac_creds.replace("/", "\\")
                from urllib.parse import urlparse as _urlparse
                _bridge_port = _urlparse(_cfg_mod.get_config("mt5_bridge_url", "")).port or 9010
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
