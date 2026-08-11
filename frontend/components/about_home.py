"""About home as a path: "Set up once" then "Every day".

Regroups the existing About sections (nothing is rewritten) so a
newcomer can tell one-time setup apart from daily use, and shows the
same daily-routine loop as Getting Started — one copy, imported, so the
two can't drift.

Pure view layer: renders content and navigates via the callback the
About page provides.
"""
from __future__ import annotations

from typing import Callable

from nicegui import ui

from frontend.components.getting_started import DAILY_ROUTINE

SETUP_ONCE: list[dict] = [
    {"section": "instructions", "icon": "menu_book", "title": "Setup Instructions",
     "desc": "Step-by-step install: CrossOver, MT5, Telegram, email and going live."},
    {"section": "registration", "icon": "how_to_reg", "title": "Registration & Setup",
     "desc": "The six numbered first-run steps, from broker account to live."},
]

EVERY_DAY: list[dict] = [
    {"section": "orchestration", "icon": "smart_toy", "title": "Bot Orchestration",
     "desc": "What each automation feature does — signals, auto-execution, DPM, pausing."},
    {"section": "glossary", "icon": "translate", "title": "Glossary",
     "desc": "Plain-English explanations of every trading term the app uses."},
    {"section": "version", "icon": "history", "title": "Version History",
     "desc": "Release notes and changelog for each version."},
]

_RISK_PARAGRAPHS = [
    ("Trading leveraged financial instruments such as gold (XAUUSD) carries a "
     "high level of risk and may not be suitable for all investors. A "
     "significant proportion of retail trader accounts lose money when trading "
     "CFDs and similar products. You should not risk capital you cannot afford "
     "to lose.", "text-xs text-orange-200 leading-relaxed"),
    ("This software is provided as-is, without warranty of any kind. It may "
     "contain bugs or produce incorrect results. Automated execution does not "
     "guarantee profitability and can result in losses that exceed your "
     "initial deposit. You are solely responsible for monitoring all open "
     "positions and for any trading decisions made, whether manually or via "
     "automation. Do not leave automated trading running unattended for "
     "extended periods without reviewing open positions.",
     "text-xs text-orange-200 leading-relaxed"),
    ("Past performance is not indicative of future results. This tool does "
     "not constitute financial advice.",
     "text-xs text-orange-300 font-semibold leading-relaxed"),
]


def _nav_card(entry: dict, show_section: Callable[[str], None]) -> None:
    with ui.card().classes(
        "flex-1 min-w-56 bg-gray-800 rounded-lg p-4 cursor-pointer "
        "hover:bg-gray-700 transition-colors self-stretch"
    ).on("click", lambda _=None, s=entry["section"]: show_section(s)):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.label(entry["icon"]).classes("material-icons text-yellow-400 text-xl")
            ui.label(entry["title"]).classes("text-sm font-semibold text-gray-100")
        ui.label(entry["desc"]).classes("text-xs text-gray-400 leading-relaxed")
        ui.label("Open →").classes("text-xs text-yellow-500 mt-2")


def render(show_section: Callable[[str], None], app_version: str) -> None:
    """Render the About home into the current container."""
    with ui.column().classes("w-full max-w-3xl gap-6 p-6"):
        with ui.row().classes("items-center gap-3"):
            ui.label("FOREX Trader").classes("text-2xl font-bold text-yellow-400")
            ui.label("by Simon Moore").classes("text-lg").style("color:#38bdf8")
            ui.badge(f"BETA v{app_version}", color="green").classes("text-xs")
        ui.separator()

        # Risk disclaimer — verbatim from the original About home.
        with ui.card().classes("w-full rounded-lg p-4").style(
            "background:#1a1008;border:1px solid #92400e;"
        ):
            with ui.row().classes("items-start gap-3"):
                ui.label("warning").classes(
                    "material-icons text-orange-400 text-xl shrink-0 mt-0.5"
                )
                with ui.column().classes("gap-2"):
                    ui.label("Risk Warning").classes(
                        "text-xs font-bold text-orange-400 uppercase tracking-wider"
                    )
                    for text, cls in _RISK_PARAGRAPHS:
                        ui.label(text).classes(cls)

        ui.label("Set up once").classes(
            "text-sm font-semibold text-gray-400 uppercase tracking-wider"
        )
        with ui.row().classes("gap-4 flex-wrap w-full items-stretch"):
            for entry in SETUP_ONCE:
                _nav_card(entry, show_section)

        ui.label("Every day").classes(
            "text-sm font-semibold text-gray-400 uppercase tracking-wider"
        )
        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
            ui.label("Your daily routine").classes(
                "text-sm font-semibold text-gray-100 mb-1"
            )
            for i, step in enumerate(DAILY_ROUTINE, start=1):
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.label(f"{i}.").classes("text-xs text-yellow-500 font-bold shrink-0")
                    ui.label(step).classes("text-xs text-gray-300 leading-relaxed")
        with ui.row().classes("gap-4 flex-wrap w-full items-stretch"):
            for entry in EVERY_DAY:
                _nav_card(entry, show_section)
