"""The three shell dialogs: Power, Pause, and the resume confirmation.

Extracted verbatim from `frontend/app/__init__.py` on 2026-09-10, which sat 11
lines under the 800-line ceiling with no seam left that did not touch the tab
layout. Nothing inside them was reshaped -- the bodies are the same code,
indented one level out and given a builder to hold them.

Each builder returns the handles the shell needs afterwards, because a NiceGUI
dialog is built where it must live in the element tree (at page root, before
the header) and opened from somewhere else entirely -- the header's Power
button, the header's paused badge.

Pinned by `tests/frontend/test_shell_dialogs.py`.
"""
from __future__ import annotations

import asyncio
import time as _time

from nicegui import app, ui

from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import trading_controller as trading_ctl


def build_power_dialog(root):
    """Restart / stop. `root` is the checkout root the relaunch runs from."""
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
            from backend.src.controllers.system_controller import restart_app
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
    return _power_dialog


def build_pause_dialog():
    """The governor's `trade_pause_until` halt: set it, clear it, explain it."""
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
                # Why, not only until when. This dialog is where the Resume
                # button lives, so it is exactly where the operator needs to
                # know which guard fired before deciding to override it.
                _why = trading_ctl.trading_halt_reason()
                if _why:
                    _pause_status_lbl.text += f" — {_why}"
                _pause_status_lbl.style("display:block")
                _resume_btn.style("display:inline-flex")
            else:
                _pause_status_lbl.style("display:none")
                _resume_btn.style("display:none")

        _pause_dialog.on_value_change(_on_pause_dialog_change)
    return _pause_dialog


def build_resume_confirm_dialog():
    """Returns (dialog, reason_label) -- the header writes the reason before it opens it."""
    # ── Resume-trading confirm popup — opened by clicking the header badge ────
    # Separate from _pause_dialog above: that dialog only knows about the
    # governor's trade_pause_until halt. The header badge also reflects the
    # circuit breaker (a different mechanism, circuit_breaker_active_until in
    # vantage_risk_settings), so this popup clears whichever of the two is
    # actually active rather than assuming it is always the governor.
    with ui.dialog() as _resume_confirm_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg min-w-80"
    ):
        ui.label("Re-enable trading?").classes("text-base font-semibold text-white mb-1")
        _resume_confirm_reason_lbl = ui.label("").classes("text-xs text-gray-400 mb-4")

        def _do_confirm_resume():
            cb = settings_ctl.get_circuit_breaker_state()
            if cb.get("is_active"):
                settings_ctl.reset_circuit_breaker()
            raw = settings_ctl.get_app_config("trade_pause_until")
            if raw is not None and float(raw or 0) > _time.time():
                settings_ctl.set_app_config("trade_pause_until", "0")
            _resume_confirm_dialog.close()
            ui.notify("Trading resumed", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button(
                "Resume Trading", icon="play_arrow",
                on_click=_do_confirm_resume,
            ).classes("bg-green-700 text-white px-4 py-2")
            ui.button("Cancel", on_click=_resume_confirm_dialog.close).classes(
                "bg-gray-700 text-white px-4 py-2"
            )
    return _resume_confirm_dialog, _resume_confirm_reason_lbl
