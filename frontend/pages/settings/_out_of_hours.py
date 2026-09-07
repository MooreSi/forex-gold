"""The Out of Hours card.

Out of Hours swaps the managing strategy during a nightly window -- the
overnight stretch when spreads widen and moves are thin. It is live:
`monitor_cycle.py:206` calls `get_effective_strategy`, and when the window is
open the OOH strategy manages the trade instead of the base one.

**Until 2026-09-07 it had no user interface.** Every field below was readable
and writable only by editing `vantage_risk_settings` directly; the sole thing
on screen was a status label on Active Trades saying whether OOH happened to
be active. A feature that changes how money is managed, configured by nothing
the owner could reach. Added when `ooh_timezone` would otherwise have become
the sixth such setting (handover/020).

Its own module rather than a sixth sub-card inside `_risk.py`, which is 493
lines and shrink-only pressure is easier to avoid than to undo.
"""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl
# Through the controller, never `backend.src.utils.models` directly:
# `frontend-reaches-the-backend-through-controllers` is enforced at ZERO
# and a direct import fails the gate. trading_controller re-exports it
# for exactly this, and _strategy_cards.py already takes the same route.
from backend.src.controllers.trading_controller import STRATEGY_NAMES

_CARD_CLASSES = "flex-1 min-w-72 bg-gray-800 p-3 rounded-lg"

# Offered as a short list rather than all ~600 IANA zones: this is a chooser,
# not a database browser, and a free-text entry is kept alongside it for
# anything not here. A zone the app cannot resolve falls back to UTC and warns
# (see risk_settings_repo._ooh_now) rather than raising -- this runs inside the
# monitor cycle, where an exception stops trade management.
_COMMON_ZONES = [
    "", "UTC", "Europe/London", "Europe/Dublin", "Europe/Lisbon",
    "Europe/Madrid", "Europe/Paris", "Europe/Berlin", "Europe/Zurich",
    "Europe/Athens", "Europe/Moscow", "America/New_York", "America/Chicago",
    "America/Denver", "America/Los_Angeles", "America/Sao_Paulo",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Hong_Kong",
    "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
]


def render_out_of_hours_card(card_classes: str = _CARD_CLASSES) -> None:
    rs = settings_ctl.get_risk_settings()

    with ui.card().classes(card_classes):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("bedtime", size="sm").classes("text-yellow-400")
            ui.label("Out of Hours").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "During this window a different strategy manages open trades — "
                "the overnight stretch when spreads widen and moves are thin. "
                "It does not stop trading; it changes which strategy is in "
                "charge. Open positions are never closed by this."
            )

        enabled = ui.switch(
            "Enable Out of Hours", value=bool(rs.get("ooh_enabled", 0)),
        ).classes("text-sm")

        with ui.row().classes("w-full gap-2 items-center mt-2"):
            start = ui.input(
                "Start", value=str(rs.get("ooh_start_time", "22:00") or "22:00"),
            ).props("dense").classes("flex-1")
            end = ui.input(
                "End", value=str(rs.get("ooh_end_time", "07:00") or "07:00"),
            ).props("dense").classes("flex-1")
        ui.label("24-hour, HH:MM. A window may span midnight.").classes(
            "text-xs text-gray-500")

        # The zone the window is read in. Stored as a NAME, not taken from the
        # machine's clock: this app runs on the owner's box and on a VPS that
        # is conventionally UTC, and reading each machine's local time would
        # have the two nodes enter Out of Hours an hour apart for the four
        # months the UK is on BST, while each looked correct on its own.
        # Blank = UTC = what every install did before this setting existed.
        tz = ui.select(
            _COMMON_ZONES,
            value=str(rs.get("ooh_timezone", "") or ""),
            label="Timezone",
            new_value_mode="add-unique",
        ).props("dense use-input").classes("w-full mt-2")
        ui.label(
            "Blank means UTC, which is what the app did before this setting "
            "existed. Set the SAME value on every machine you run — two "
            "machines on different zones will disagree about when the window "
            "opens."
        ).classes("text-xs text-gray-500")

        strategy = ui.select(
            dict(STRATEGY_NAMES),
            value=str(rs.get("ooh_strategy", "conservative") or "conservative"),
            label="Strategy",
        ).props("dense").classes("w-full mt-2")

        with ui.expansion("Holiday dates", icon="event").classes("w-full text-sm mt-2"):
            ui.label(
                "When on, Out of Hours applies ALL DAY on every date in the "
                "range, and not at all outside it. Dates are read in the "
                "timezone above."
            ).classes("text-xs text-gray-500 mb-2")
            date_active = ui.switch(
                "Use a date range", value=bool(rs.get("ooh_date_active", 0)),
            ).classes("text-sm")
            with ui.row().classes("w-full gap-2 items-center"):
                date_from = ui.input(
                    "From", value=str(rs.get("ooh_date_from", "") or ""),
                ).props("dense").classes("flex-1")
                date_to = ui.input(
                    "To", value=str(rs.get("ooh_date_to", "") or ""),
                ).props("dense").classes("flex-1")
            ui.label("YYYY-MM-DD").classes("text-xs text-gray-500")

        def save_ooh():
            try:
                settings_ctl.update_risk_settings({
                    "ooh_enabled":     int(bool(enabled.value)),
                    "ooh_start_time":  str(start.value or "22:00").strip(),
                    "ooh_end_time":    str(end.value or "07:00").strip(),
                    "ooh_timezone":    str(tz.value or "").strip(),
                    "ooh_strategy":    str(strategy.value or "conservative"),
                    "ooh_date_active": int(bool(date_active.value)),
                    "ooh_date_from":   str(date_from.value or "").strip(),
                    "ooh_date_to":     str(date_to.value or "").strip(),
                })
                ui.notify("Out of Hours settings saved", type="positive")
            except (TypeError, ValueError) as _save_err:
                ui.notify(f"Invalid value — {_save_err}", type="negative")

        ui.button("Save Out of Hours", on_click=save_ooh).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )
