"""Signal Generator — the Breakout and Reversal Engine tabs.

This module used to host the Bounce generator too, and most of it WAS that
panel. Bounce was removed on the owner's instruction (2026-09-02); its
`_sections.py` and `_shared.py` went with it, since nothing outside the panel
used them.

The bounce SERVICE still exists and keeps its slot in
`engines_controller._ENGINE_SERVICES` -- the sync server and the mode toggle
bind engines by that fixed order -- but it is excluded from
`start_stopped_engines()`, so it cannot run with no panel to show that it is
running. See tests/frontend/test_bounce_generator_removed.py.
"""
from __future__ import annotations

from typing import Callable

from nicegui import ui

from backend.src.controllers import engines_controller as engines_controller


import logging

_log = logging.getLogger(__name__)

_STARTING_BALANCE = 1000.0


# ── Main render ───────────────────────────────────────────────────────────────

def render(get_engine: Callable) -> None:
    """Entry point — renders the Breakout and Reversal Engine tabs.

    Bounce was removed 2026-09-02 on the owner's instruction. Its service is
    still present but is excluded from engines_controller.start_stopped_engines,
    so it cannot be started with no panel to show that it is running.
    """
    from frontend.pages import breakout_panel
    from frontend.pages import reversal_panel

    with ui.tabs().classes("bg-gray-900 border-b border-gray-700") as sg_tabs:
        t_breakout = ui.tab("Breakout", icon="trending_up")
        t_reversal_engine  = ui.tab("Reversal Engine",  icon="content_copy")

    # animated=False: Quasar's slide transition tracks each panel's position via
    # an internally registered index tied to component identity. NiceGUI re-keys
    # elements on every content rebuild (this page's periodic refresh loops
    # rebuild large chunks of each panel's content), so that index goes stale —
    # confirmed live: switching to Reversal Engine left "aria-selected" correctly true
    # on the Reversal Engine tab while the DOM kept rendering Bounce's content
    # underneath, indefinitely, not just a brief flash. Disabling the animation
    # removes the transform-based positioning calculation that gets this wrong.
    with ui.tab_panels(sg_tabs, value=t_breakout).props("animated=false").classes("bg-gray-900 w-full").style("padding:0"):
        with ui.tab_panel(t_breakout).style("padding:0"):
            breakout_panel.render()
        with ui.tab_panel(t_reversal_engine).style("padding:0"):
            reversal_panel.render()
