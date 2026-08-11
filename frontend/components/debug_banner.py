"""The debug-mode banner — unmistakable "this is not real" signage.

Rendered by the app shell above the header whenever config.is_debug()
(the shell owns the check; this component only draws). Amber on dark so
it cannot be confused with the profit/loss greens and reds.
"""
from __future__ import annotations

from nicegui import ui


def render_debug_banner() -> None:
    with ui.row().classes(
        "w-full items-center justify-center gap-2 py-1"
    ).style("background:#78350f; border-bottom:2px solid #f59e0b;"):
        ui.icon("science").classes("text-amber-300 text-base")
        ui.label(
            "DEBUG MODE — simulated data, no real orders. "
            "Prices, signals and AI text are fakes."
        ).classes("text-xs font-bold text-amber-200 tracking-wide")
