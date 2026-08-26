"""
FOREX Trader — NiceGUI application entry point.
Initialises the engine and reader on startup, then defines the main page layout.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

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

import backend.src.config as cfg_module
from backend.src.utils.version_history import __version__ as _APP_VERSION
from backend.src.controllers import settings_controller as settings_ctl
from frontend.pages import backtest as backtest_page
from backend.src.utils.version_history import __version__ as _APP_VERSION
from frontend.pages import news as news_page

log = logging.getLogger(__name__)

# Admin panel availability + engine lifecycle now live in core/app_lifecycle.py
# so the headless entry point sees the same logic without importing NiceGUI.
from backend.src.app import (          # noqa: E402
    admin_open_fn as _admin_open_fn, ADMIN_AVAILABLE,
    get_engine, get_tg_reader,
    startup as _lifecycle_startup, shutdown as _lifecycle_shutdown,
)

# Resolved from the repo marker, not by counting parents off __file__:
# this module moved from frontend/app.py to frontend/app/__init__.py and
# a fixed .parent silently pointed at frontend/app/static, which does not
# exist. See os_utils.repo_root for the four other modules this bit.
from backend.src.utils.os_utils import repo_root as _repo_root  # noqa: E402

STATIC_DIR = _repo_root() / "frontend" / "static"
app.add_static_files("/static", str(STATIC_DIR))

# Versioned favicon URL — Safari caches favicons in its own database keyed by
# URL, so a plain data URL or /favicon.ico won't bust its cache. Using the
# startup timestamp as a query string produces a URL Safari has never seen on
# each app restart, forcing it (and all other browsers) to fetch the new icon.
import time as _time

from ._about import _render_about
_FAVICON_VERSION = int(_time.time())
_FAVICON_HTML = (
    f'<link rel="icon" type="image/png" href="/static/favicon.png?v={_FAVICON_VERSION}">'
    if (STATIC_DIR / "favicon.png").exists() else ""
)

# ── Cash register sound — Web Audio API synthesis ────────────────────────────
# Played in the browser when a trade closes with a profit.
#
# IMPORTANT — browser autoplay policy:
#   AudioContext starts in 'suspended' state unless the user has interacted with
#   the page.  _VC_AUDIO_UNLOCK_JS must be injected once on page load; it attaches
#   click/keydown/touchstart listeners that create and resume the shared context.
#   _CASH_REGISTER_JS then reuses that already-running context.
#
_VC_AUDIO_UNLOCK_JS = """
<script>
(function() {
    window._vcAudioCtx = null;
    function _vcUnlock() {
        if (!window._vcAudioCtx) {
            try { window._vcAudioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
            catch(e) { return; }
        }
        if (window._vcAudioCtx.state === 'suspended') {
            window._vcAudioCtx.resume().catch(function(){});
        }
    }
    ['click','keydown','touchstart'].forEach(function(ev) {
        document.addEventListener(ev, _vcUnlock, {passive: true});
    });
})();
</script>
"""

_CASH_REGISTER_JS = """
(function() {
    try {
        var ctx = window._vcAudioCtx;
        if (!ctx || ctx.state !== 'running') return;
        var t = ctx.currentTime;

        // Mechanical click — very short filtered noise burst
        var sr  = ctx.sampleRate;
        var len = Math.floor(sr * 0.025);
        var buf = ctx.createBuffer(1, len, sr);
        var d   = buf.getChannelData(0);
        for (var i = 0; i < len; i++) {
            d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 2);
        }
        var click     = ctx.createBufferSource();
        var clickGain = ctx.createGain();
        click.buffer  = buf;
        clickGain.gain.setValueAtTime(0.5, t);
        click.connect(clickGain);
        clickGain.connect(ctx.destination);
        click.start(t);

        // Metallic "ching" ring — three harmonics, exponential decay
        [[659, 0.35, 0.7], [1319, 0.22, 0.45], [1976, 0.12, 0.3]].forEach(function(h) {
            var osc  = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type            = 'sine';
            osc.frequency.value = h[0];
            gain.gain.setValueAtTime(h[1], t + 0.008);
            gain.gain.exponentialRampToValueAtTime(0.0001, t + h[2]);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(t + 0.008);
            osc.stop(t + h[2]);
        });
    } catch(e) {}
})();
"""

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

    # ── Compact ticker strip ───────────────────────────────────────────────────
    _prev_bid: list[Optional[float]] = [None]
    _price_hist: list[float]         = []

    with ui.row().classes(
        "w-full items-center bg-gray-900 border-b border-gray-700 px-3 gap-0"
    ).style("height:54px; min-height:54px;"):

        # Banner / logo
        banner = STATIC_DIR / "banner.png"
        if banner.exists():
            ui.image("/static/banner.png").classes("h-10 object-contain pr-3 shrink-0")
        else:
            with ui.column().classes("pr-3 gap-0 justify-center shrink-0"):
                ui.label("FOREX Trader").classes(
                    "text-sm font-bold text-yellow-400 leading-none"
                )
                ui.label("by Simon Moore").classes(
                    "text-xs leading-none"
                ).style("color:#38bdf8")
                ui.label(f"BETA Version {_APP_VERSION}").classes(
                    "text-xs leading-none font-semibold"
                ).style("color:#4ade80; font-size:9px;")

        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")

        # Stat helper — single line: LABEL VALUE, wrapped so both share the same
        # flex baseline and font-mono metrics don't shift the value down.
        def _stat_inline(label: str, init: str = "$—", cls: str = "text-white"):
            with ui.row().classes("items-center gap-1 shrink-0 pr-3"):
                ui.label(label).classes("text-xs text-gray-500 leading-none shrink-0")
                val = ui.label(init).classes(
                    f"text-xs font-mono font-semibold leading-none {cls}"
                )
            return val

        bid_val    = _stat_inline("BID")
        ask_val    = _stat_inline("ASK")
        spread_lbl = ui.label("spr:—").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        bal_val  = _stat_inline("MT5 BAL")
        fm_lbl   = ui.label("").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        eq_val   = _stat_inline("EQUITY")
        eq_sub   = ui.label("").classes("text-xs pr-3 leading-none")

        # ── GitHub "Update Available" badge (2026-08-01) ─────────────────────
        # Hidden entirely when no update is available -- only ever shown once
        # a git fetch against origin/main confirms new commits. Click opens a
        # popup that says in plain English what the update changes (core_app_
        # update.summarise_changes, falling back to the commit subjects when
        # no AI provider is configured or the call fails), with the raw commit
        # list collapsed underneath; confirming pulls, reinstalls dependencies,
        # and restarts (core_app_update.py / platform_utils.restart_app, the
        # same relaunch mechanism the Power dialog uses).
        update_badge = ui.button("Update Available", icon="new_releases").props(
            "dense unelevated color=green"
        ).classes("text-xs shrink-0 ml-1 animate-pulse")
        update_badge.set_visibility(False)
        _pending_update: dict = {"commits": []}
        # Cached AI summary, keyed by the remote SHA it describes, so reopening
        # the popup doesn't pay for another LLM call for the same update.
        _update_summary_cache: dict = {"sha": "", "bullets": [], "error": ""}
        _update_summary_seq = [0]  # guards against a stale in-flight summary

        with ui.dialog() as _update_dialog, ui.card().classes(
            "bg-gray-800 p-5 rounded-lg min-w-96 max-w-lg"
        ):
            ui.label("Update Available").classes("text-base font-semibold text-white mb-1")
            _update_dialog_sub = ui.label("").classes("text-xs text-gray-400 mb-2")
            # What's in the update, in plain English (core_app_update.
            # summarise_changes). The raw commit list lives below it, collapsed.
            _update_summary_body = ui.column().classes("w-full gap-1")
            with ui.expansion("Commit list").props("dense").classes(
                "w-full text-xs text-gray-400 mt-2"
            ):
                _update_dialog_body = ui.column().classes(
                    "w-full gap-0.5 max-h-72 overflow-y-auto"
                )
            _update_dialog_status = ui.label("").classes("text-xs mt-2")

            async def _do_apply_update():
                _update_dialog_status.text = "Updating — pulling latest code, reinstalling dependencies..."
                _update_dialog_status.classes(replace="text-xs text-orange-300 mt-2")
                from backend.src.services.positions import core_app_update
                result = await core_app_update.apply_update()
                if not result["ok"]:
                    _update_dialog_status.text = f"Update failed: {result['error']}"
                    _update_dialog_status.classes(replace="text-xs text-red-400 mt-2")
                    return
                ui.notify("Update applied — restarting...", type="positive")
                from backend.src.utils.os_utils import restart_app
                await asyncio.sleep(1)
                restart_app(root)

            with ui.row().classes("gap-2 mt-3 justify-end"):
                ui.button("Cancel", on_click=_update_dialog.close).props("flat")
                ui.button("Update Now", on_click=_do_apply_update).classes(
                    "bg-green-700 text-white"
                )

        def _update_summary_target() -> str:
            return _pending_update.get("remote_sha", "") or ""

        def _draw_update_summary(bullets: list[str], error: str) -> None:
            """Fill the summary section: the AI bullets when we have them,
            otherwise the commit subjects, which are still a truer answer to
            "what changed?" than a bare "an update is available"."""
            _update_summary_body.clear()
            commits = _pending_update.get("commits") or []
            with _update_summary_body:
                if bullets:
                    for b in bullets:
                        with ui.row().classes("items-start gap-2 no-wrap"):
                            ui.label("•").classes("text-xs text-green-400 leading-relaxed")
                            ui.label(b).classes("text-xs text-gray-200 leading-relaxed")
                    return
                for c in commits:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.label("•").classes("text-xs text-green-400 leading-relaxed")
                        ui.label(c["summary"]).classes("text-xs text-gray-200 leading-relaxed")
                if not commits:
                    ui.label(
                        "The commit list for this update could not be read."
                    ).classes("text-xs text-gray-400")
                if error:
                    ui.label(f"Plain-English summary unavailable — {error}").classes(
                        "text-xs text-gray-500 italic mt-1"
                    )

        async def _load_update_summary():
            remote_sha = _update_summary_target()
            _update_summary_seq[0] += 1
            seq = _update_summary_seq[0]
            from backend.src.services.positions import core_app_update
            try:
                bullets, error = await core_app_update.summarise_changes(
                    _pending_update.get("local_sha", ""), remote_sha,
                )
            except Exception as e:  # summarising must never block updating
                bullets, error = [], f"{type(e).__name__}: {e}"
            if seq != _update_summary_seq[0]:
                return  # a newer open/check superseded this one
            _update_summary_cache.update(
                {"sha": remote_sha, "bullets": bullets, "error": error}
            )
            _draw_update_summary(bullets, error)

        async def _open_update_dialog():
            commits = _pending_update.get("commits") or []
            _update_dialog_sub.text = (
                f"{len(commits)} new commit(s) from github.com/MooreSi/forex."
                if commits else "An update is ready from github.com/MooreSi/forex."
            )

            _update_dialog_body.clear()
            with _update_dialog_body:
                for c in commits:
                    ui.label(f"{c['short_sha']}  {c['summary']}").classes(
                        "text-xs font-mono text-gray-300 leading-relaxed"
                    )

            remote_sha = _update_summary_target()
            cached = bool(remote_sha) and _update_summary_cache["sha"] == remote_sha
            if cached:
                _draw_update_summary(
                    _update_summary_cache["bullets"], _update_summary_cache["error"]
                )
            else:
                _update_summary_body.clear()
                with _update_summary_body:
                    ui.label("Summarising what's changed...").classes(
                        "text-xs text-gray-400 italic"
                    )

            _update_dialog_status.text = ""
            _update_dialog.open()
            if not cached:
                await _load_update_summary()

        update_badge.on("click", _open_update_dialog)

        async def _check_github_update():
            from backend.src.services.positions import core_app_update
            try:
                result = await core_app_update.check_for_update()
            except Exception:
                return
            _pending_update["commits"]   = result.get("commits", [])
            _pending_update["local_sha"] = result.get("local_sha", "")
            _pending_update["remote_sha"] = result.get("remote_sha", "")
            update_badge.set_visibility(bool(result.get("available")))

        ui.timer(2.0, _check_github_update, once=True)
        ui.timer(600.0, _check_github_update)  # re-check every 10 minutes

        ui.space()

        # Sparkline
        sparkline = ui.echart({
            "backgroundColor": "transparent",
            "animation": False,
            "grid": {"left": 0, "right": 0, "top": 2, "bottom": 2},
            "xAxis": {"type": "category", "show": False, "data": []},
            "yAxis": {"type": "value",    "show": False, "scale": True},
            "series": [{
                "type": "line", "data": [], "showSymbol": False,
                "lineStyle": {"color": "#00CC88", "width": 1.5},
                "areaStyle": {"color": "rgba(0,204,136,0.12)"},
            }],
        }).classes("w-20 shrink-0").style("height:28px")

        acct_lbl    = ui.label(
            "XAUUSD Gold Live"
            if cfg_module.get("account_env", "demo") == "live"
            else "XAUUSD Simulation"
        ).classes("text-xs text-gray-500 px-2 shrink-0")
        conn_badge  = ui.badge("MT5 —", color="grey").classes("text-xs shrink-0")
        ea_badge    = ui.badge("EA", color="grey").classes("text-xs shrink-0 ml-1")
        mode_btn = ui.button("LOCAL").props("dense outline size=sm color=amber").classes(
            "text-xs ml-1 shrink-0"
        ).style("min-height:20px; padding:0 8px;").tooltip(
            "Local/Remote sync — switch which node is actively trading"
        )
        pause_badge = ui.badge("⏸ PAUSED", color="orange").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")
        news_badge = ui.badge("📰 NEWS EVENT", color="red").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")

        # ── Local/Remote mode toggle ─────────────────────────────────────────
        # Replaces the old strategy badge. Switching modes runs the
        # STAND_DOWN/RESUME handshake over the sync channel (see
        # forex_trader/sync/) so exactly one node ever executes new trades.
        # This node's own sub-engines (breakout/bounce/reversal_engine) are fully
        # stopped/started as part of the switch; the Telegram reader itself
        # keeps running either way to avoid MTProto reconnect churn, but
        # SimulationEngine.open_trade() refuses every new order while stood
        # down regardless — the reader being "warm" cannot execute anything.
        _mode_switching = [False]

        def _mode_sub_engines():
            # Function-local on purpose: defers the engine imports past app
            # boot, exactly as the direct service imports here always did.
            from backend.src.controllers import engines_controller
            return engines_controller.sub_engines()

        async def _refresh_mode_btn():
            if _mode_switching[0]:
                return
            from backend.src.controllers import sync_controller as sync_ctl
            if not sync_ctl.is_connected():
                mode_btn.text = "LOCAL"
                mode_btn.props("color=grey")
                mode_btn.tooltip(
                    "No remote node connected — trading locally by default. "
                    "Configure one in Settings > Remote Node."
                )
                return
            active = settings_ctl.get_active_trader()
            if active == "local":
                mode_btn.text = "LOCAL"
                mode_btn.props("color=amber")
                mode_btn.tooltip("This machine is actively trading. Click to hand control back to the VPS.")
            else:
                mode_btn.text = "REMOTE"
                mode_btn.props("color=blue")
                mode_btn.tooltip("VPS is actively trading; this is a view-only dashboard. Click to take over.")

        async def _toggle_mode():
            from backend.src.controllers import sync_controller as sync_ctl
            if not sync_ctl.is_connected():
                ui.notify("Not connected to a remote node — configure one in "
                          "Settings > Remote Node first.", type="warning")
                return
            if _mode_switching[0]:
                return
            _mode_switching[0] = True
            mode_btn.text = "SWITCHING…"
            mode_btn.props("color=grey")
            mode_btn.disable()
            try:
                current = settings_ctl.get_active_trader()
                if current != "local":
                    # Remote -> Local: take over. Must succeed on the VPS
                    # side (its ack) before this node starts trading.
                    try:
                        ack = await sync_ctl.request_stand_down(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge stand-down: {exc}", type="negative")
                        return
                    settings_ctl.set_active_trader("local")
                    from backend.src.controllers import engines_controller as _engines_ctl
                    _engines_ctl.start_stopped_engines()
                    _n_open = len(ack.get("open_positions", []))
                    ui.notify(
                        f"Now trading locally. VPS stood down"
                        + (f" ({_n_open} of its position(s) still running to their own SL/TP)" if _n_open else "")
                        + ".", type="positive",
                    )
                else:
                    # Local -> Remote: any already-open local positions keep
                    # being managed by this node's own _monitor_loop (SL/TP,
                    # trailing, etc.) regardless of active_trader — that gate
                    # only blocks *new* signals from opening, it doesn't stop
                    # this node from managing trades it already has open. So
                    # switching doesn't abandon them; the user can also just
                    # close them manually in MT5/the broker terminal directly.
                    engine = get_engine()
                    open_trades = engine.get_open_trades()
                    from backend.src.controllers import engines_controller as _engines_ctl
                    _engines_ctl.stop_running_engines()
                    try:
                        await sync_ctl.request_resume(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge resume: {exc}", type="negative")
                        # Engines are already stopped locally; leave them stopped
                        # rather than guess whether the VPS actually resumed.
                        settings_ctl.set_active_trader("remote_vps")
                        return
                    settings_ctl.set_active_trader("remote_vps")
                    ui.notify(
                        "Control handed back to the VPS."
                        + (f" ({len(open_trades)} local position(s) still running to their own SL/TP)"
                           if open_trades else "")
                        + " This is now view-only.", type="positive",
                    )
            finally:
                _mode_switching[0] = False
                mode_btn.enable()
                await _refresh_mode_btn()

        mode_btn.on_click(_toggle_mode)

        # ── Help button — opens Getting Started (wired after tabs exist) ───────
        _help_open: list = [None]
        ui.button(
            icon="help_outline",
            on_click=lambda: _help_open[0]() if _help_open[0] else None,
        ).classes("ml-2 shrink-0").style(
            "background:transparent; color:#38bdf8; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Help — Getting Started")

        # ── Pause button ───────────────────────────────────────────────────────
        ui.button(icon="pause_circle", on_click=_pause_dialog.open).classes(
            "ml-2 shrink-0"
        ).style(
            "background:transparent; color:#fbbf24; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Pause / Resume trading")

        # ── Power button ───────────────────────────────────────────────────────
        ui.button(icon="power_settings_new", on_click=_power_dialog.open).classes(
            "ml-1 shrink-0"
        ).style(
            "background:transparent; color:#4ade80; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Power options — Restart / Stop")

        # ── Admin button (far right — only visible when KeyGen is present) ──────
        if ADMIN_AVAILABLE:
            ui.button(icon="admin_panel_settings", on_click=_admin_open_fn).classes(
                "ml-2 shrink-0"
            ).style(
                "background:transparent; color:#facc15; min-width:32px; min-height:32px; "
                "width:32px; height:32px; padding:0;"
            ).tooltip("Admin panel (password protected)")

    _last_profit_seq  = [None]   # None = not yet initialised; int = last seen seq
    # Initial-deposit cache: sum of all MT5 balance-credit deals.
    # Fetched once on startup then refreshed every 5 minutes.
    # Used to compute total P&L = equity - net_deposited.
    _net_deposited    = [None]   # None = not yet fetched
    _deposit_fetch_at = [0.0]
    # News event state — tracks when we entered the current news window so we
    # send exactly one Telegram alert per event, not one every 2 seconds.
    _news_in_window      = [False]   # True while inside a news blackout window
    _news_alerted_window = [None]    # window_start timestamp of the last alert sent

    async def _refresh_header():
        try:
            import time as _time
            engine = get_engine()
            tick   = await engine.get_tick()

            # Pause badge
            pause_raw = settings_ctl.get_app_config("trade_pause_until")
            is_paused = pause_raw and float(pause_raw or 0) > _time.time()
            pause_badge.style("" if is_paused else "display:none")

            # News event badge — check live calendar; send one Telegram alert on entry
            try:
                from backend.src.services.test_signal.news_filter import get_current_event as _get_news_event
                _news_ev = _get_news_event()
                if _news_ev:
                    import datetime as _dt_news
                    _resume_utc = _dt_news.datetime.fromtimestamp(
                        _news_ev["window_end"], tz=_dt_news.timezone.utc
                    ).strftime("%H:%M UTC")
                    _pause_mins = int(round(_news_ev["mins_remaining"]))
                    news_badge.text  = f"📰 NEWS EVENT — resumes {_resume_utc}"
                    news_badge.style("")
                    # Send Telegram alert once per unique event window
                    _ws = _news_ev["window_start"]
                    if not _news_in_window[0] or _news_alerted_window[0] != _ws:
                        _news_in_window[0]      = True
                        _news_alerted_window[0] = _ws
                        try:
                            from backend.src.services.telegram import alerts as _tg
                            import datetime as _dt2
                            _ev_time = _dt2.datetime.fromtimestamp(
                                _news_ev["event_ts"], tz=_dt2.timezone.utc
                            ).strftime("%H:%M UTC")
                            _mins_pre = int(round(max(0, _news_ev["mins_to_event"])))
                            _tg_msg = (
                                f"⚠️ *NEWS EVENT — Trading Paused*\n\n"
                                f"📰 *{_news_ev['title']}* ({_news_ev['currency']})\n"
                                f"🕐 Scheduled: *{_ev_time}*\n"
                                f"⏱ Paused for: *{_pause_mins} minutes*\n"
                                f"✅ Resumes at: *{_resume_utc}*\n\n"
                                f"_All signal generators continue running. "
                                f"No MT5 orders will be placed until the window clears._"
                            )
                            await _tg.send_message(_tg_msg, event_type="news_pause")
                        except Exception as _news_alert_exc:
                            log.warning("news-pause Telegram alert failed: %s", _news_alert_exc)
                else:
                    news_badge.style("display:none")
                    _news_in_window[0] = False
            except Exception:
                news_badge.style("display:none")

            if tick:
                prev = _prev_bid[0]
                bid_val.text    = f"${tick.bid:,.2f}"
                ask_val.text    = f"${tick.ask:,.2f}"
                spread_lbl.text = f"spr:{tick.spread_points:.0f}pt"
                if prev is not None:
                    chg = tick.bid - prev
                    col = "text-green-400" if chg >= 0 else "text-red-400"
                    bid_val.classes(replace=f"text-xs font-mono font-semibold {col} pr-3")
                _prev_bid[0] = tick.bid
                _price_hist.append(tick.bid)
                if len(_price_hist) > 60:
                    _price_hist.pop(0)
                sparkline.options["xAxis"]["data"]     = list(range(len(_price_hist)))
                sparkline.options["series"][0]["data"] = list(_price_hist)
                sparkline.update()

            mt5_acc = await engine.get_mt5_account()
            if mt5_acc:
                bal      = float(mt5_acc.get("balance", 0) or 0)
                eq       = float(mt5_acc.get("equity",  0) or 0)
                fm       = float(mt5_acc.get("margin_free", 0) or 0)
                bal_val.text = f"${bal:,.2f}"
                fm_lbl.text  = f"free:${fm:,.0f}"
                eq_val.text  = f"${eq:,.2f}"

                # Refresh initial deposit every 5 minutes.
                # Sum all MT5 balance-credit deals (type=2, positive profit).
                # Net deposits = total credits - total withdrawals.
                import time as _t2
                now_m = _t2.monotonic()
                if _net_deposited[0] is None or now_m - _deposit_fetch_at[0] > 300:
                    try:
                        all_deals = await engine._bridge.get_deal_history(3650)
                        credits = sum(
                            float(d.get("profit", 0))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) > 0
                        )
                        debits = sum(
                            abs(float(d.get("profit", 0)))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) < 0
                        )
                        if credits > 0:
                            _net_deposited[0] = credits - debits
                            _deposit_fetch_at[0] = now_m
                    except Exception as _dep_exc:
                        # Header P&L keeps its last value — say so in the log
                        # instead of silently showing a stale figure.
                        log.warning("net-deposit refresh failed (header P&L may be stale): %s",
                                    _dep_exc)

                if _net_deposited[0]:
                    total_pnl = eq - _net_deposited[0]
                    col = "text-green-400" if total_pnl >= 0 else "text-red-400"
                    eq_sub.text = f"P&L:${total_pnl:+.2f}"
                    eq_sub.classes(replace=f"text-xs pr-3 {col}")

            health = await engine.get_bridge_health()
            if health.get("connected"):
                if health.get("trade_allowed") is False:
                    conn_badge.props("color=orange")
                    conn_badge.text = "MT5: AutoTrading OFF"
                    conn_badge.tooltip(
                        "AutoTrading is disabled in MetaTrader 5. "
                        "Click the AutoTrading button (robot icon) in the MT5 toolbar to enable it "
                        "before placing trades."
                    )
                else:
                    conn_badge.props("color=green")
                    conn_badge.text = "MT5 Connected"
            else:
                conn_badge.props("color=red")
                conn_badge.text = "MT5 Disconnected"

            # EA badge — logic lives in ea_bridge.get_effective_ea_status()
            # (testable, and shared if any other page ever needs it) so this
            # stays a thin render step. That heartbeat/health check already
            # refreshes on its own (sync heartbeat every 3s; local EABridge
            # health computed fresh here), so no extra polling is needed.
            try:
                from backend.src.services.broker import ea_bridge as _ea_bridge_mod
                ea_ok, ea_scope = _ea_bridge_mod.get_effective_ea_status()
                ea_badge.props(f"color={'green' if ea_ok else 'red'}")
                ea_badge.tooltip(
                    f"EA connected on {ea_scope} — trades can be managed natively in MT5"
                    if ea_ok else
                    f"EA not connected on {ea_scope} — trades still work, "
                    "falling back to Python-managed instead of native on-tick management"
                )
            except Exception as _ea_exc:
                log.debug("EA badge refresh failed: %s", _ea_exc)

            await _refresh_mode_btn()

            cfg = cfg_module.load()
            env = cfg.get("account_env", "demo")
            acct_lbl.text = (
                "XAUUSD Gold Live" if env == "live" else "XAUUSD Simulation"
            )

            # Play cash register sound when a trade closes with profit.
            # On the first tick we silently snapshot the current count so the
            # sound only fires for profitable closes that happen *during* this
            # browser session, not for historical ones.
            current_profit_seq = engine._profit_sound_seq
            if _last_profit_seq[0] is None:
                _last_profit_seq[0] = current_profit_seq
            elif current_profit_seq > _last_profit_seq[0]:
                _last_profit_seq[0] = current_profit_seq
                await ui.run_javascript(_CASH_REGISTER_JS)

        except Exception as _hdr_exc:
            # One bad tick must not kill the 2s header timer, but a broken
            # header refresh showing stale balances silently is worse — log it.
            log.warning("header refresh failed (values on screen may be stale): %s", _hdr_exc)

    ui.timer(2.0, _refresh_header)

    # ── Demo / Live env-switch ────────────────────────────────────────────────
    _cur_env   = cfg_module.get("account_env", "demo")
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
        from backend.src.config import DATA_DIR as _DATA_DIR
        settings_ctl.switch_environment_db(str(_DATA_DIR / f"forex_trader_{new_env}.db"))
        cfg_module.save_to_yaml({"account_env": new_env})
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
            if new_env == cfg_module.get("account_env", "demo"):
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
        lambda: cfg_module.get("account_env", "demo") != "live",
    )
    if _start_here.should_show(app.storage.user):
        asyncio.ensure_future(open_start_here())

    # ── Help "?" → Getting Started (frontend/components/getting_started.py) ──
    from frontend.components import getting_started as _getting_started
    _help_open[0] = _getting_started.attach(
        tabs, tab_about, _about_nav, open_start_here
    )
