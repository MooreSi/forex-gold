"""Settings → Security. Who can open this app on this machine.

One setting today: whether a restart asks for the dashboard password or comes
straight in (owner's request, 2026-09-02).

Deliberately its own tab rather than tucked into Registration or Theme. The
question "does this machine ask for a password" is the first thing someone
looks for when they want to change it, and burying an access control inside an
unrelated section is how it stays forgotten.
"""
from __future__ import annotations

from nicegui import ui

from backend.src.controllers import settings_controller as cfg_module

_REQUIRE = "require"
_AUTO = "auto"


def _render_security() -> None:
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mb-4"):
        ui.label("App Access").classes("font-bold text-yellow-300 mb-1")
        ui.label(
            "What happens when the app restarts."
        ).classes("text-xs text-gray-400 mb-3")

        current = _AUTO if cfg_module.get_config("auto_login_enabled", False) \
            else _REQUIRE

        mode = ui.radio(
            {
                _REQUIRE: "Ask for the dashboard password",
                _AUTO:    "Log in automatically",
            },
            value=current,
        ).props("dense").classes("text-sm")

        warn = ui.label("").classes("text-xs text-orange-400 mt-1")

        def _warn_for(value) -> None:
            warn.text = (
                "Anyone who can open this machine can place and close live "
                "trades without a password."
                if value == _AUTO else ""
            )

        _warn_for(current)
        mode.on_value_change(lambda e: _warn_for(e.value))

        def _save() -> None:
            cfg_module.save_config(
                {"auto_login_enabled": bool(mode.value == _AUTO)})
            ui.notify(
                "Automatic login enabled — no password on restart"
                if mode.value == _AUTO
                else "The dashboard password will be required on restart",
                type="positive",
            )

        ui.button("Save", icon="save", on_click=_save).classes(
            "bg-blue-700 text-white text-xs px-3 py-1 mt-3"
        )
