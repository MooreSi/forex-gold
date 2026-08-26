"""Settings page — MT5 credentials, API keys, risk settings."""

import asyncio
from typing import Callable

from nicegui import ui


from ._diagnostics import _render_diagnostics
from ._email import _render_email
from ._ai import _render_ai
from ._appearance import _render_registration, _render_theme
from ._mt5 import _render_mt5
from ._risk import render_risk_card
from ._telegram import _render_tg_bot

# The package's whole public surface. render() is the page; render_risk_card
# is rendered by the trading page too. Everything else is an internal of a
# section module and is imported by name where it is needed.
__all__ = ["render", "render_risk_card"]

# ── Prevent-sleep state (module-level so it survives page re-renders) ──────────
# On macOS uses `caffeinate -i -w <app-pid>`.
# On Windows uses SetThreadExecutionState via platform_utils.

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


# ── Registration tab ───────────────────────────────────────────────────────────

