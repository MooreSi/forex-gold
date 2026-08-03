"""Settings > Expert Tunables (M7).

A generic renderer over the Expert Tunables catalogue. It deliberately
knows nothing about any individual setting: it draws whatever the settings
controller hands back, so a new tunable appears here by being added to the
catalogue in backend/src/services/risk/expert_params.py.

That is the entire point. The reason ~135 behaviour constants ended up
hardcoded is that exposing one previously meant designing a widget, wiring
a save, and finding somewhere to put it. Here it costs one catalogue entry.

Lives in its own module rather than inside settings.py because that file is
already 3,000+ lines and over the LOC ceiling -- see FINISH_LINE.md.
"""
from nicegui import ui

from backend.src.controllers.settings import controller as settings_ctl


def render():
    """Generic renderer over the Expert Tunables catalogue.

    Deliberately knows nothing about any individual setting: it draws
    whatever the controller hands back, so a new tunable appears here by
    being added to the catalogue in services/risk/expert_params.py. That
    is the whole point -- the reason ~135 behaviour constants were
    hardcoded in the first place is that each one would otherwise have
    needed its own bespoke widget.
    """
    ui.label("Expert Tunables").classes("text-xl font-bold text-yellow-300")
    ui.markdown(
        "Behaviour values that used to be hardcoded. **Every default here is "
        "exactly what the app used before this page existed** — nothing "
        "changes until you move a dial.\n\n"
        "Several of these gate order placement (the R:R floor, the "
        "directional cap, the broker-close threshold). Each is clamped to a "
        "safe range, but a legal value can still be a bad one — change them "
        "one at a time and watch the result."
    ).classes("text-sm text-gray-400 w-full")

    body = ui.column().classes("w-full gap-4")

    def _draw():
        body.clear()
        catalogue = settings_ctl.get_expert_param_catalogue()
        with body:
            for domain, rows in catalogue.items():
                with ui.card().classes("w-full bg-gray-800"):
                    ui.label(domain).classes("text-lg font-bold text-yellow-200")
                    for row in rows:
                        _draw_row(row, _draw)
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    "Reset all to defaults",
                    on_click=lambda: _reset_all(_draw),
                ).props("flat color=red")

    def _draw_row(row, redraw):
        modified = row["value"] != row["default"]
        with ui.row().classes("w-full items-center gap-3 py-1"):
            with ui.column().classes("flex-grow gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(row["label"]).classes("text-sm text-white")
                    if modified:
                        ui.badge("modified").props("color=orange")
                ui.label(row["desc"]).classes("text-xs text-gray-400")
            field = ui.number(
                value=row["value"],
                min=row["min"],
                max=row["max"],
                step=1 if row["integer"] else 0.05,
            ).props("dense outlined dark").classes("w-32")
            ui.label(row["unit"]).classes("text-xs text-gray-500 w-12")
            ui.label(f"default {row['default']}").classes("text-xs text-gray-500 w-28")

            def _save(_e=None, key=row["key"], f=field):
                if f.value is None:
                    return
                settings_ctl.save_expert_params({key: f.value})
                redraw()
                ui.notify(f"Saved {key}", color="positive")

            field.on("blur", _save)
            ui.button(
                icon="restart_alt",
                on_click=lambda _e=None, key=row["key"]: _reset_one(key, redraw),
            ).props("flat dense round").tooltip("Reset to default")

    def _reset_one(key, redraw):
        settings_ctl.reset_expert_param(key)
        redraw()
        ui.notify(f"{key} reset to default", color="positive")

    def _reset_all(redraw):
        settings_ctl.reset_all_expert_params()
        redraw()
        ui.notify("All Expert Tunables reset to defaults", color="positive")

    _draw()


