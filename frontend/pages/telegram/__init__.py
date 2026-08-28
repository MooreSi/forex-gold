"""Telegram page — auth wizard, group selector, live message feed."""

from typing import Callable

from nicegui import ui

from backend.src.controllers.telegram_controller import (
    AUTH_DISCONNECTED, AUTH_AWAITING_CODE, AUTH_AWAITING_2FA,
    AUTH_CONNECTED, AUTH_RECONNECTING, AUTH_FAILED,
)
from frontend.pages.trading import render_signals_card

from ._auth import (
    _render_connected,
    _render_send_code_step,
    _render_verify_2fa_step,
    _render_verify_code_step,
)
from ._feed import _render_channels_active_section
from ._keywords import _render_parsing_settings_section

__all__ = ["render"]

def render(get_tg_reader: Callable):
    reader = get_tg_reader()

    render_signals_card()
    _render_parsing_settings_section()
    _render_channels_active_section(reader)

    # ── Status banner ──────────────────────────────────────────────────────────
    status_badge = ui.badge("Disconnected", color="red").classes("text-sm mb-3")
    error_lbl    = ui.label("").classes("text-red-300 text-sm")

    def _update_status_badge():
        state = reader.auth_state
        err   = reader.auth_error or ""
        colours = {
            AUTH_DISCONNECTED:  ("red",    "Disconnected"),
            AUTH_AWAITING_CODE: ("orange", "Awaiting Code"),
            AUTH_AWAITING_2FA:  ("orange", "Awaiting 2FA"),
            AUTH_CONNECTED:     ("green",  "Connected"),
            AUTH_RECONNECTING:  ("blue",   "Reconnecting"),
            AUTH_FAILED:        ("red",    "Failed"),
        }
        colour, label = colours.get(state, ("grey", state))
        status_badge.props(f"color={colour}")
        status_badge.text  = label
        # Hide top badge when connected — the inline badge next to Disconnect shows instead
        status_badge.style("display:none" if state == AUTH_CONNECTED else "")
        error_lbl.text     = err

    # ── Auth wizard (re-renders on each state change) ─────────────────────────
    main_container   = ui.column().classes("w-full")
    _rendered_state  = [None]

    def _render_wizard():
        state = reader.auth_state
        _update_status_badge()
        if state == _rendered_state[0]:
            return
        _rendered_state[0] = state
        main_container.clear()
        with main_container:
            if state == AUTH_CONNECTED:
                ui.notify("Telegram connected!", type="positive")
                _render_connected(reader)
            else:
                with ui.card().classes("w-full max-w-lg bg-gray-800 p-6 rounded-lg mt-4"):
                    ui.label("Telegram Authentication").classes(
                        "text-lg font-bold text-yellow-300 mb-4"
                    )
                    if state in (AUTH_DISCONNECTED, AUTH_FAILED):
                        _render_send_code_step(reader, _render_wizard)
                    elif state == AUTH_AWAITING_CODE:
                        _render_verify_code_step(reader, _render_wizard)
                    elif state == AUTH_AWAITING_2FA:
                        _render_verify_2fa_step(reader, _render_wizard)

    ui.timer(1.0, _render_wizard)
    _render_wizard()


