"""Shared empty-state component — every empty surface says what to do next.

"No signals yet" is a dead end; "No signals yet — they arrive from
Telegram channels (Parsing tab), or build one yourself in Build Signal"
teaches the causal chain at the exact moment the user is looking for it.

The copy lives here so pages stay consistent and tests can pin it.
Pure view layer: no controllers, no engine.
"""
from __future__ import annotations

from typing import Optional

from nicegui import ui

EMPTY_STATES: dict[str, dict] = {
    "tg_signals": {
        "icon": "satellite_alt",
        "message": "No Telegram signals yet.",
        "next_step": (
            "Signals arrive from Telegram channels — connect a channel under "
            "the Parsing tab — or build one yourself in Trading → Build Signal."
        ),
    },
    "closed_trades": {
        "icon": "history",
        "message": "No closed trades in this period.",
        "next_step": (
            "Trades appear here after they close. Open positions live on the "
            "Trading tab; widen the date range to see older history."
        ),
    },
    "day_trades": {
        "icon": "event_busy",
        "message": "No trades on this day.",
        "next_step": "Pick a coloured day on the calendar to see its trades.",
    },
}


def spec(key: str) -> dict:
    """The copy for one surface; unknown keys fail loudly (KeyError)."""
    return EMPTY_STATES[key]


def render_empty_state(key: str, compact: bool = False) -> None:
    """Render the empty state for ``key`` into the current container."""
    s = spec(key)
    with ui.column().classes(
        "items-center gap-1 " + ("py-2" if compact else "py-6 w-full")
    ):
        ui.icon(s["icon"]).classes("text-gray-600 text-3xl")
        ui.label(s["message"]).classes("text-sm text-gray-400")
        ui.label(s["next_step"]).classes(
            "text-xs text-gray-500 leading-relaxed text-center"
        ).style("max-width:420px")
