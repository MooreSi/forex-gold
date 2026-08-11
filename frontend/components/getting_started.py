"""Getting Started — the header Help "?" surface.

The app already ships good guidance (Setup Instructions, Registration,
Bot Orchestration, the Glossary) but it lives behind the About tab's nav
cards where a newcomer never finds it. This dialog links that existing
content — it duplicates none of it — and offers the way back into the
Start Here checklist after it has been dismissed.

Pure view layer: navigation callbacks only, no controllers, no engine.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping

from nicegui import ui

# Each entry links an existing About section by its real dispatch id
# (frontend/app.py _show_section). tests/frontend/test_help.py pins that
# every id here still exists there.
GUIDES: list[dict] = [
    {"section": "instructions", "icon": "menu_book", "title": "Setup Instructions",
     "blurb": "Step-by-step install and connection guide: MT5, the bridge, "
              "Telegram, email — everything you set up once."},
    {"section": "registration", "icon": "how_to_reg", "title": "Registration & Setup",
     "blurb": "The six numbered first-run steps, from broker account to live."},
    {"section": "orchestration", "icon": "smart_toy", "title": "How the bot works",
     "blurb": "What each automation feature does — signals, auto-execution, "
              "position management, pausing."},
    {"section": "glossary", "icon": "translate", "title": "Glossary",
     "blurb": "Plain-English meaning of every trading term the app uses "
              "(R:R, SL, TP, ADX, DPM, Anchor, Trail…)."},
]

DAILY_ROUTINE: list[str] = [
    "Check the header: MT5 Connected (green) and no pause/news badge.",
    "Review pending signals and open positions on the Trading tab.",
    "Glance at Analysis for how today's closed trades went.",
]


def attach(
    tabs: Any,
    tab_about: Any,
    about_nav: Mapping[str, Callable[..., None]],
    open_start_here: Callable[[], Any],
) -> Callable[[], None]:
    """Build the Getting Started dialog and return ``open_getting_started``.

    ``about_nav`` is filled by the About renderer with its ``show_section``
    callable; clicking a guide jumps to the About tab and opens that section.
    """
    with ui.dialog() as dialog, ui.card().classes(
        "bg-gray-900 p-5 rounded-lg w-full"
    ).style("max-width:640px"):
        with ui.column().classes("w-full gap-3"):
            ui.label("Getting Started").classes("text-xl font-bold text-yellow-400")

            with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
                ui.label("New here? Run the setup checklist").classes(
                    "text-sm font-semibold text-gray-100"
                )
                ui.label(
                    "Six live checks — licence, MT5, risk — each with a "
                    "Fix-this jump to the right place."
                ).classes("text-xs text-gray-400 leading-relaxed")

                def _open_checklist() -> None:
                    dialog.close()
                    asyncio.ensure_future(open_start_here())

                ui.button("Open the Start Here checklist", on_click=_open_checklist).classes(
                    "bg-yellow-700 text-white text-xs px-3 py-1 mt-2"
                )

            ui.label("Your daily routine").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            for i, step in enumerate(DAILY_ROUTINE, start=1):
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.label(f"{i}.").classes("text-xs text-yellow-500 font-bold shrink-0")
                    ui.label(step).classes("text-xs text-gray-300 leading-relaxed")

            ui.label("Guides").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            for guide in GUIDES:
                def _open_guide(_=None, section=guide["section"]) -> None:
                    dialog.close()
                    tabs.set_value(tab_about)
                    show = about_nav.get("show_section")
                    if show is not None:
                        show(section)

                with ui.row().classes(
                    "w-full items-center gap-3 bg-gray-800 rounded-lg p-3 "
                    "cursor-pointer hover:bg-gray-700 transition-colors"
                ).on("click", _open_guide):
                    ui.icon(guide["icon"]).classes("text-yellow-400 text-xl shrink-0")
                    with ui.column().classes("gap-0.5 flex-1"):
                        ui.label(guide["title"]).classes("text-sm font-semibold text-gray-100")
                        ui.label(guide["blurb"]).classes("text-xs text-gray-400 leading-relaxed")
                    ui.label("Open →").classes("text-xs text-yellow-500 shrink-0")

            with ui.row().classes("w-full justify-end"):
                ui.button("Close", on_click=dialog.close).classes(
                    "bg-gray-700 text-gray-300 text-xs px-3 py-1"
                )

    return dialog.open
