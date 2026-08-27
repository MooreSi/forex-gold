"""
FOREX Trader — NiceGUI application entry point.
Initialises the engine and reader on startup, then defines the main page layout.
"""

import asyncio
import logging
import sys
from pathlib import Path

from nicegui import app, ui

# ── NiceGUI 3.12.x timer teardown patch ───────────────────────────────────────
# In NiceGUI 3.12.1, element.parent_slot raises RuntimeError (via dead weakref)
# when the page client disconnects and the slot is GC'd.
# elements/timer._get_context() calls parent_slot expecting None-or-slot, but
# gets an exception instead, producing log spam on every page navigation.
# Patch both methods so disconnect is silent.
from contextlib import nullcontext as _nullcontext
from nicegui.elements.timer import Timer as _UITimer
from nicegui.timer import Timer as _BaseTimer


def _safe_timer_get_context(self):
    try:
        return self.parent_slot or _nullcontext()
    except RuntimeError:
        self.cancel()
        return _nullcontext()


def _safe_timer_cleanup(self):
    _BaseTimer._cleanup(self)  # clears self.callback
    if not self._deleted:
        try:
            slot = self._parent_slot() if self._parent_slot else None
            if slot is not None:
                slot.parent.remove(self)
        except Exception:
            pass


_UITimer._get_context = _safe_timer_get_context
_UITimer._cleanup = _safe_timer_cleanup
# ── end patch ──────────────────────────────────────────────────────────────────

# ── WebSocket buffer size patch ─────────────────────────────────────────────────
# python-socketio/engine.io default to a 1,000,000-byte (1MB) max message size in
# both directions. A long-lived real account's full trade history / equity curve
# (History tab, "days=3650") is exactly the kind of single-payload push that can
# grow past that over months of trading, and the client then refuses to send/
# receive it at all -- "Message too long: the message is too large for WebSocket
# transmission", followed by a reconnect loop, not a graceful degradation.
# NiceGUI doesn't expose this as a ui.run() kwarg; core.sio is the same
# process-wide socketio.AsyncServer every client connection shares, so this must
# be set once, here, before any client connects.
from nicegui import core as _nicegui_core
_nicegui_core.sio.eio.max_http_buffer_size = 10_000_000  # 10MB, was 1MB
# ── end patch ──────────────────────────────────────────────────────────────────

from backend.src.controllers import settings_controller as cfg_module
from backend.src.controllers import settings_controller as settings_ctl
from frontend.pages import backtest as backtest_page
from frontend.pages import news as news_page

log = logging.getLogger(__name__)

# Admin panel availability + engine lifecycle now live in core/app_lifecycle.py
# so the headless entry point sees the same logic without importing NiceGUI.
from backend.src.app import (          # noqa: E402
    get_engine, get_tg_reader,
    startup as _lifecycle_startup, shutdown as _lifecycle_shutdown,
)

from ._shared import _VC_AUDIO_UNLOCK_JS, STATIC_DIR  # noqa: E402

app.add_static_files("/static", str(STATIC_DIR))

# Versioned favicon URL — Safari caches favicons in its own database keyed by
# URL, so a plain data URL or /favicon.ico won't bust its cache. Using the
# startup timestamp as a query string produces a URL Safari has never seen on
# each app restart, forcing it (and all other browsers) to fetch the new icon.
import time as _time

from ._about import _render_about

from ._header import build_header
_FAVICON_VERSION = int(_time.time())
_FAVICON_HTML = (
    f'<link rel="icon" type="image/png" href="/static/favicon.png?v={_FAVICON_VERSION}">'
    if (STATIC_DIR / "favicon.png").exists() else ""
)

# ── App-wide singletons ────────────────────────────────────────────────────────
# Engine/bridge/Telegram/sync lifecycle lives in core/app_lifecycle.py so the
# headless entry point (run_headless.py) can call the exact same startup()/
# shutdown() without importing NiceGUI. These just wire it to NiceGUI's hooks.

@app.on_startup
async def startup():
    await _lifecycle_startup()


@app.on_shutdown
async def shutdown():
    await _lifecycle_shutdown()


# ── Main page ──────────────────────────────────────────────────────────────────

@ui.page("/")
def main_page():
    import os
    import subprocess
    import sys
    import time as _time
    from frontend.pages import trading, telegram, history, settings as settings_page
    import frontend.pages.chart as chart_page
    import frontend.pages.ai_summary as ai_summary_page
    import frontend.pages.test_panel as test_panel

    ui.query("body").style(
        "background: #0f1117; color: #e0e0e0; font-family: 'Inter', sans-serif; margin:0;"
    )

    ui.add_head_html(
        '<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>'
    )
    if _FAVICON_HTML:
        ui.add_head_html(_FAVICON_HTML)
    # Unlock Web Audio API on first user interaction so the cash-register sound
    # can play from timer callbacks (which are not user gestures).
    ui.add_head_html(_VC_AUDIO_UNLOCK_JS)

    # Settings > Theme -- override CSS is static (all presets), the active
    # preset is picked via a data attribute set inline before first paint
    # (avoids a flash of the wrong theme).
    from frontend.theme import THEME_HEAD_CSS, get_theme
    ui.add_head_html(THEME_HEAD_CSS)
    ui.add_head_html(f'<script>document.documentElement.setAttribute("data-fx-theme","{get_theme()}")</script>')

    # The checkout root, found by marker rather than parent count. Three
    # parents was correct from forex_trader/ui/app.py; this file is two levels
    # deep at frontend/app.py, so the fixed count resolved to the directory
    # ABOVE the repo and the restart button relaunched "/Users/simon/run.py" --
    # which does not exist, so the app shut down and never came back
    # (restart.log, 2026-08-26). Same bug class as the five path counts fixed
    # earlier in this merge; the marker cannot drift on the next move.
    from backend.src.utils.os_utils import repo_root as _repo_root
    root = _repo_root()

    # ── Power dialog (defined BEFORE header so it renders at root level) ────────
    with ui.dialog() as _power_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg min-w-72"
    ):
        ui.label("Power Options").classes("text-base font-semibold text-white mb-1")
        ui.label(
            "Restart relaunches the app. Your browser reconnects automatically in ~5 s."
        ).classes("text-xs text-gray-400 mb-4")

        async def _do_restart():
            _power_dialog.close()
            ui.notify("Restarting — browser will reconnect in ~5 seconds...", type="info")
            from backend.src.utils.os_utils import restart_app
            await asyncio.sleep(1)
            restart_app(root)

        async def _do_stop():
            _power_dialog.close()
            ui.notify("Shutting down FOREX Trader...", type="warning")
            await asyncio.sleep(1)
            app.shutdown()

        with ui.row().classes("gap-2"):
            ui.button("Restart", icon="refresh", on_click=_do_restart).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Stop", icon="power_off", on_click=_do_stop).classes(
                "bg-red-700 text-white px-4 py-2"
            )
            ui.button("Cancel", on_click=_power_dialog.close).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

    # ── Pause dialog (defined BEFORE header) ─────────────────────────────────
    with ui.dialog() as _pause_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg min-w-80"
    ):
        ui.label("Pause Trading").classes("text-base font-semibold text-yellow-300 mb-1")
        ui.label(
            "While paused, all signal generators and Telegram signals continue to run normally "
            "but no orders will be sent to MT5. "
            "Active trade management (SL/TP monitoring) continues as normal."
        ).classes("text-xs text-gray-400 mb-4")

        # Status label — refreshed each time the dialog opens
        _pause_status_lbl = ui.label("").classes(
            "text-yellow-400 text-sm font-semibold mb-3"
        ).style("display:none")

        ui.label("Pause for:").classes("text-sm text-gray-300 mb-1")
        pause_hours = ui.number("Hours", value=4, min=0.25, max=168, step=0.25, format="%.2f").classes("w-full")
        pause_hours.tooltip("Number of hours to pause trading. 0.25 = 15 minutes.")

        ui.label("Or pause until a specific time (local time):").classes("text-sm text-gray-300 mt-3 mb-1")
        pause_until_inp = ui.input(
            "Date & time (YYYY-MM-DD HH:MM)",
            value="",
        ).classes("w-full")
        pause_until_inp.tooltip(
            "Enter a specific date and time to pause until. "
            "Leave blank to use the hours field above."
        )

        pause_result = ui.label("").classes("text-xs text-gray-400 mt-2")

        async def _do_pause():
            from datetime import datetime as _dt
            try:
                if pause_until_inp.value.strip():
                    # User types local time; strptime gives a naive local datetime
                    dt_local = _dt.strptime(pause_until_inp.value.strip(), "%Y-%m-%d %H:%M")
                    pause_ts = dt_local.timestamp()
                else:
                    pause_ts = _time.time() + float(pause_hours.value or 4) * 3600

                if pause_ts <= _time.time():
                    pause_result.text = "Pause time must be in the future"
                    return

                settings_ctl.set_app_config("trade_pause_until", str(pause_ts))
                exp = _dt.fromtimestamp(pause_ts)
                _pause_dialog.close()
                ui.notify(
                    f"Trading paused until {exp.strftime('%d %b %H:%M')}",
                    type="warning",
                )
            except Exception as e:
                pause_result.text = f"Error: {e}"

        def _do_resume():
            settings_ctl.set_app_config("trade_pause_until", "0")
            _pause_dialog.close()
            ui.notify("Trading resumed", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button(
                "Pause Now", icon="pause",
                on_click=_do_pause,
            ).classes("bg-yellow-700 text-white px-4 py-2")
            _resume_btn = ui.button(
                "Resume Trading", icon="play_arrow",
                on_click=_do_resume,
            ).classes("bg-green-700 text-white px-4 py-2").style("display:none")
            ui.button("Cancel", on_click=_pause_dialog.close).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

        def _on_pause_dialog_change(e):
            """Refresh status label and Resume button whenever the dialog opens."""
            if not e.value:
                return
            from datetime import datetime as _dt
            raw = settings_ctl.get_app_config("trade_pause_until")
            paused = raw is not None and float(raw or 0) > _time.time()
            if paused:
                try:
                    exp = _dt.fromtimestamp(float(raw))
                    _pause_status_lbl.text = f"Currently PAUSED until {exp.strftime('%d %b %Y %H:%M')}"
                except Exception:
                    _pause_status_lbl.text = "Currently PAUSED"
                _pause_status_lbl.style("display:block")
                _resume_btn.style("display:inline-flex")
            else:
                _pause_status_lbl.style("display:none")
                _resume_btn.style("display:none")

        _pause_dialog.on_value_change(_on_pause_dialog_change)

    # ── Debug banner — above everything when running on fakes ─────────────────
    if cfg_module.is_debug():
        from frontend.components.debug_banner import render_debug_banner
        render_debug_banner()

    _help_open = build_header(
        power_dialog=_power_dialog,
        pause_dialog=_pause_dialog,
        root=root,
    )


    # ── Demo / Live env-switch ────────────────────────────────────────────────
    _cur_env   = cfg_module.get_config("account_env", "demo")
    _is_live   = [_cur_env == "live"]   # mutable — updated after confirmed switch
    _reverting = [False]                # guards against recursive toggle events

    # ── No-credentials dialog (generic) ──────────────────────────────────────
    with ui.dialog() as _no_creds_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg max-w-md"
    ):
        _no_creds_title = ui.label("").classes("text-lg font-bold text-yellow-300 mb-2")
        _no_creds_body  = ui.label("").classes(
            "text-sm text-gray-300 mb-4 whitespace-pre-line leading-relaxed"
        )
        ui.button("OK", on_click=_no_creds_dialog.close).classes(
            "bg-gray-700 text-white px-4 py-2"
        )

    # ── MT5 account reminder dialog — shown after successful switch ───────────
    with ui.dialog() as _mt5_remind_dialog, ui.card().classes(
        "bg-gray-800 p-6 rounded-lg max-w-md"
    ):
        _mt5_remind_title = ui.label("").classes("text-lg font-bold mb-2")
        _mt5_remind_body  = ui.label("").classes(
            "text-sm text-gray-300 mb-3 leading-relaxed whitespace-pre-line"
        )
        _mt5_remind_srv   = ui.label("").classes(
            "text-xs font-mono bg-gray-900 rounded p-2 mb-4 text-yellow-300"
        )

        async def _mt5_remind_ok():
            _mt5_remind_dialog.close()
            await asyncio.sleep(0.2)
            await ui.run_javascript("window.location.reload()")

        ui.button(
            "OK — I've switched the account", icon="check",
            on_click=_mt5_remind_ok,
        ).classes("bg-green-700 text-white px-4 py-2 w-full")

    # ── Live confirmation dialog ──────────────────────────────────────────────
    with ui.dialog() as _live_confirm_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg max-w-md"
    ):
        ui.label("Switch to LIVE Trading").classes(
            "text-lg font-bold text-red-400 mb-2"
        )
        ui.label(
            "This will reconnect the MT5 bridge to your LIVE Vantage Markets account "
            "and switch all data to the live database.\n\nReal funds will be at risk."
        ).classes("text-sm text-gray-300 mb-4 whitespace-pre-line leading-relaxed")

        async def _confirm_live():
            _live_confirm_dialog.close()
            await _do_env_switch("live")

        def _cancel_live():
            _live_confirm_dialog.close()
            _reverting[0] = True
            env_switch.value = False
            _reverting[0] = False

        with ui.row().classes("gap-2"):
            ui.button(
                "Confirm — Switch to Live", icon="warning",
                on_click=_confirm_live,
            ).classes("bg-red-700 text-white px-4 py-2")
            ui.button("Cancel", on_click=_cancel_live).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

    async def _do_env_switch(new_env: str):
        """Switch env: save config + swap DB, reconnect bridge, show MT5 reminder, reload."""
        engine = get_engine()
        # Always read from master credential store (demo DB) — env-independent
        creds = settings_ctl.get_mt5_credentials()

        if new_env == "live":
            login    = int(creds.get("live_login") or 0)
            password = creds.get("live_password_enc") or ""
            server   = (creds.get("live_server") or "").strip()
        else:
            login    = int(creds.get("login") or 0)
            password = creds.get("password_enc") or ""
            server   = (creds.get("server") or "").strip()

        if not login or not password or not server:
            env_label = "Live" if new_env == "live" else "Demo"
            _no_creds_title.text = f"{env_label} Credentials Not Configured"
            _no_creds_body.text  = (
                f"No {env_label} MT5 credentials are saved.\n\n"
                "Go to Settings > MT5 / Bridge, enter your "
                f"{env_label} account login, password and server, "
                "then try switching again."
            )
            _no_creds_dialog.open()
            _reverting[0] = True
            env_switch.value = _is_live[0]
            _reverting[0] = False
            return

        # 1. Swap DB and persist new env immediately
        from backend.src.controllers.settings_controller import DATA_DIR as _DATA_DIR
        settings_ctl.switch_environment_db(str(_DATA_DIR / f"forex_trader_{new_env}.db"))
        cfg_module.save_config({"account_env": new_env})
        _is_live[0] = (new_env == "live")

        # 2. Write bridge_credentials.json so bridge connects correctly on restart
        settings_ctl.sync_bridge_credentials_file(new_env)

        # 3. Tell the bridge to switch to the target account, auto-recover if needed
        bridge_note      = ""
        autotrading_note = ""
        try:
            result = await engine._bridge.send_credentials(login, password, server)
            if result.get("status") == "connected":
                # /credentials already calls enable_autotrading() internally.
                # If it still failed (bridge older build or Win32 error), show a note.
                at = result.get("autotrading") or {}
                if not at.get("enabled") and not result.get("trade_allowed"):
                    # Older bridge builds don't return autotrading field — try explicitly
                    if "autotrading" not in result:
                        at = await engine._bridge.enable_autotrading()
                    if not at.get("enabled"):
                        at_err = at.get("error", "unknown")
                        autotrading_note = (
                            f"\n\nAlgo Trading could not be enabled automatically ({at_err}). "
                            "Please open MetaTrader 5 and click the AutoTrading (robot) button "
                            "in the toolbar."
                        )
            else:
                # Bridge saved credentials but MT5 login failed — try /reconnect
                err = result.get("error") or result.get("status") or "unknown"
                try:
                    reconnect_result = await engine._bridge.reconnect()
                    if reconnect_result.get("status") == "connected":
                        bridge_note = "\n\nBridge reconnected successfully."
                        at = await engine._bridge.enable_autotrading()
                        if not at.get("enabled"):
                            at_err = at.get("error", "unknown")
                            autotrading_note = (
                                f"\n\nAlgo Trading could not be enabled automatically ({at_err}). "
                                "Please open MetaTrader 5 and click the AutoTrading (robot) "
                                "button in the toolbar."
                            )
                    else:
                        reconnect_err = reconnect_result.get("error", "unknown error")
                        bridge_note = (
                            f"\n\nBridge could not reconnect to MT5: {reconnect_err}.\n"
                            "Go to Settings > MT5 / Bridge and click Start Bridge."
                        )
                except Exception as reconnect_exc:
                    bridge_note = (
                        f"\n\nBridge is offline: {reconnect_exc}.\n"
                        "Go to Settings > MT5 / Bridge and click Start Bridge."
                    )
        except Exception as _be:
            bridge_note = (
                f"\n\nCould not reach the bridge: {_be}.\n"
                "Go to Settings > MT5 / Bridge and click Start Bridge if it is not running."
            )

        # 4. Show MT5 account reminder — user clicks OK to trigger reload
        extra = bridge_note + autotrading_note
        if new_env == "live":
            _mt5_remind_title.text = "Switched to LIVE Trading"
            _mt5_remind_title.classes(replace="text-lg font-bold text-red-400 mb-2")
            _mt5_remind_body.text = (
                "The app is now in LIVE mode.\n\n"
                "Please ensure MetaTrader 5 is logged into your "
                "LIVE account before clicking OK. "
                "All displayed data will reflect the live account once you proceed."
                + extra
            )
        else:
            _mt5_remind_title.text = "Switched to Simulation / Demo"
            _mt5_remind_title.classes(replace="text-lg font-bold text-blue-400 mb-2")
            _mt5_remind_body.text = (
                "The app is now in DEMO / Simulation mode.\n\n"
                "Please ensure MetaTrader 5 is logged into your "
                "DEMO account before clicking OK."
                + extra
            )
        _mt5_remind_srv.text = f"MT5 Server:  {server}"
        _mt5_remind_dialog.open()

    # ── Tab navigation row  (switch ← | tabs) ────────────────────────────────
    with ui.row().classes(
        "w-full bg-gray-900 border-b border-gray-700 items-stretch"
    ).style("min-height:48px"):

        # Demo / Live toggle — far left
        with ui.row().classes(
            "items-center gap-1.5 px-3 shrink-0"
        ).style("border-right:1px solid #374151"):
            demo_lbl = ui.label("Demo").classes(
                "text-xs font-semibold leading-none "
                + ("text-blue-400" if not _is_live[0] else "text-gray-600")
            )
            env_switch = ui.switch("").props("dense color=red").style(
                "transform:scale(0.85);"
            )
            env_switch.value = _is_live[0]
            live_lbl = ui.label("Live").classes(
                "text-xs font-semibold leading-none "
                + ("text-red-400" if _is_live[0] else "text-gray-600")
            )

        def _on_env_switch(e):
            if _reverting[0]:
                return
            # NiceGUI 3.x on_value_change gives ValueChangeEventArguments with .value
            new_live = bool(e.value)
            new_env  = "live" if new_live else "demo"
            if new_env == cfg_module.get_config("account_env", "demo"):
                return
            if new_live:
                _live_confirm_dialog.open()
            else:
                asyncio.ensure_future(_do_env_switch("demo"))

        # Use on_value_change (NiceGUI 3.x API) — on("update:model-value") passes e.value=None
        env_switch.on_value_change(_on_env_switch)

        # Tabs fill the rest of the row. Each carries a plain-language
        # subtitle as its tooltip — see frontend/components/tab_labels.py.
        from frontend.components.tab_labels import TAB_SUBTITLES
        with ui.tabs().classes("flex-1 bg-transparent") as tabs:
            tab_ai       = ui.tab("AI Analysis", icon="smart_toy")
            tab_chart    = ui.tab("Chart",       icon="candlestick_chart")
            tab_trading  = ui.tab("Trading",     icon="trending_up")
            tab_telegram = ui.tab("Parsing",     icon="send")
            tab_test     = ui.tab("Signal Generator", icon="science")
            tab_backtest = ui.tab("Backtest",    icon="bar_chart")
            tab_history  = ui.tab("Analysis",    icon="history")
            tab_settings = ui.tab("Settings",    icon="settings")
            tab_news     = ui.tab("News",        icon="newspaper")
            tab_about    = ui.tab("About",       icon="info")
            for _tab in (tab_ai, tab_chart, tab_trading, tab_telegram, tab_test,
                         tab_backtest, tab_history, tab_settings, tab_about):
                _tab.tooltip(TAB_SUBTITLES.get(_tab._props.get("name", ""), ""))

        # ── Circuit breaker — global, live-trades-only indicator ────────────
        # The only circuit breaker that ever blocks a real MT5 order (see
        # core/database.py's get_circuit_breaker_state/record_live_trade_
        # outcome) — signal generators used to each show their own separate
        # virtual-P&L threshold badge, which had nothing to do with live
        # execution and just duplicated/confused this one. Single indicator,
        # always visible, next to About.
        with ui.row().classes(
            "items-center gap-1.5 px-3 shrink-0"
        ).style("border-left:1px solid #374151"):
            cb_icon = ui.icon("shield", size="xs").classes("text-green-400")
            cb_top_lbl = ui.label("Circuit Breaker OK").classes(
                "text-xs font-semibold leading-none text-green-400"
            )

        def _refresh_cb_badge():
            try:
                cb = settings_ctl.get_circuit_breaker_state()
            except Exception:
                return
            if cb.get("is_active"):
                rem = int(cb.get("remaining_secs", 0))
                hms = f"{rem // 3600:02d}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"
                cb_icon.classes(replace="text-red-400")
                cb_icon.props("name=block")
                cb_top_lbl.text = f"Circuit Breaker Active — resumes in {hms}"
                cb_top_lbl.classes(replace="text-xs font-semibold leading-none text-red-400")
            else:
                cb_icon.classes(replace="text-green-400")
                cb_icon.props("name=shield")
                cb_top_lbl.text = "Circuit Breaker OK"
                cb_top_lbl.classes(replace="text-xs font-semibold leading-none text-green-400")

        ui.timer(5.0, _refresh_cb_badge)
        _refresh_cb_badge()

    def _on_tab_change(e):
        if e.value != "History":
            return
        # Only celebrate if today's closed-trade P&L is positive
        cutoff = _time.time() - 86400
        try:
            daily_pnl = settings_ctl.fetch_realised_pnl_last_24h(cutoff)
        except Exception:
            daily_pnl = 0.0
        if daily_pnl <= 0:
            return
        ui.run_javascript("""
                (function() {
                    const gold   = ['#f59e0b', '#fbbf24', '#fde68a', '#d97706'];
                    const colors = ['#f59e0b', '#10b981', '#60a5fa', '#a78bfa', '#f472b6', '#fbbf24'];
                    const opts = {
                        particleCount: 80,
                        spread: 100,
                        startVelocity: 45,
                        ticks: 200,
                        colors: colors,
                        origin: { y: 0.75 }
                    };
                    // Centre burst
                    confetti({ ...opts, origin: { x: 0.5, y: 0.75 } });
                    // Left cannon after 400 ms
                    setTimeout(() => confetti({
                        particleCount: 60, angle: 60, spread: 70,
                        startVelocity: 50, ticks: 180,
                        colors: gold, origin: { x: 0.05, y: 0.8 }
                    }), 400);
                    // Right cannon after 400 ms
                    setTimeout(() => confetti({
                        particleCount: 60, angle: 120, spread: 70,
                        startVelocity: 50, ticks: 180,
                        colors: gold, origin: { x: 0.95, y: 0.8 }
                    }), 400);
                    // Second centre burst at 1.4 s
                    setTimeout(() => confetti({
                        particleCount: 50, spread: 120,
                        startVelocity: 35, ticks: 150,
                        colors: colors, origin: { x: 0.5, y: 0.6 }
                    }), 1400);
                    // Final wide shower at 2.2 s
                    setTimeout(() => confetti({
                        particleCount: 70, spread: 160,
                        startVelocity: 25, ticks: 120,
                        colors: colors, origin: { x: 0.5, y: 0.5 },
                        gravity: 0.6, scalar: 0.9
                    }), 2200);
                })();
            """)

    tabs.on_value_change(_on_tab_change)

    # animated=False — see the same fix on the Signal Generator sub-tabs
    # (test_panel.py) for why: Quasar's slide transition can get stuck showing
    # the previous tab's content indefinitely after content elsewhere on the
    # page rebuilds, not just as a brief flash.
    with ui.tab_panels(tabs, value=tab_chart).props("animated=false").classes("w-full flex-1 bg-gray-900"):
        with ui.tab_panel(tab_ai):
            ai_summary_page.render(get_engine)
        with ui.tab_panel(tab_chart):
            chart_page.render(get_engine)
        with ui.tab_panel(tab_trading):
            trading.render(get_engine, get_tg_reader)
        with ui.tab_panel(tab_telegram):
            telegram.render(get_tg_reader)
        with ui.tab_panel(tab_history).style("padding:0"):
            history.render(get_engine)
        with ui.tab_panel(tab_settings):
            settings_page.render(get_engine, get_tg_reader)
        with ui.tab_panel(tab_backtest):
            backtest_page.render(get_engine)
        with ui.tab_panel(tab_news):
            news_page.render()
        with ui.tab_panel(tab_about):
            _about_nav: dict = {}
            _render_about(_about_nav)
        with ui.tab_panel(tab_test):
            test_panel.render(get_engine)

    # ── Start Here — first-run checklist (frontend/components/start_here.py) ──
    from frontend.components import start_here as _start_here
    open_start_here = _start_here.attach(
        tabs, {"Trading": tab_trading, "Settings": tab_settings},
        get_engine, get_tg_reader,
        lambda: cfg_module.get_config("account_env", "demo") != "live",
    )
    if _start_here.should_show(app.storage.user):
        asyncio.ensure_future(open_start_here())

    # ── Help "?" → Getting Started (frontend/components/getting_started.py) ──
    from frontend.components import getting_started as _getting_started
    _help_open[0] = _getting_started.attach(
        tabs, tab_about, _about_nav, open_start_here
    )
