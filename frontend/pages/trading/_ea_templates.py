"""EA trade templates — the per-channel template library.

A template fully replaces strategy dispatch for its channel by design,
so it wins over format-triggered defaults too.
"""
from typing import Optional
from nicegui import ui


def _render_ea_templates_card() -> None:
    """
    EA Templates: complete, self-contained, EA-managed trade-management
    definitions (Grid vs Single, TP/SL visibility, trailing method,
    breakeven rule, cancel-pending-siblings) -- a channel can be assigned a
    saved template in the Channel Strategy card below in place of a
    built-in strategy. Unlike Strategy Parameters above (which only
    retunes existing Python-managed strategies), a template fully
    replaces strategy dispatch and the EA manages the trade end-to-end --
    every field here is sent fresh on each open, so changing a template's
    values never needs an EA recompile. See core_ea_templates.py's module
    docstring. Harvest moved to Global Parameters (below) 2026-07-24 --
    it now applies account-wide to every open position regardless of how
    it was opened, not just this template's own trades. Anchor TP (added
    2026-07-24): a per-TP pips/pct ladder -- pips fill any level the raw
    signal didn't supply, pct always wins over the signal (which never
    states a close percentage) -- see core_open_trade.py's EA-handoff block.
    """
    from backend.src.services.broker import ea_templates as et

    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("EA Templates").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Complete EA-managed trade definitions -- assign one to a channel "
            "(Channel Strategy, right) in place of a built-in strategy. The EA "
            "reads every field fresh on each open, no recompile needed."
        )

    state = {"name": None}  # None = new/unsaved template
    fields: dict[str, object] = {}

    body = ui.column().classes("w-full gap-2")

    def _load(name: Optional[str]) -> None:
        state["name"] = name
        _draw_body()

    def _current_values() -> dict:
        out = {}
        for k, f in fields.items():
            v = f.value
            if isinstance(v, dict):  # NiceGUI dict-options select
                v = v.get("value")
            out[k] = v
        return out

    def _draw_body() -> None:
        body.clear()
        live = (et.get_ea_template(state["name"]) if state["name"] else None) or dict(et.DEFAULTS)
        fields.clear()
        with body:
            with ui.row().classes("items-center gap-2 mb-2"):
                existing = et.list_ea_templates()
                load_opts = {"": "— New Template —"}
                load_opts.update({t["name"]: t["name"] for t in existing})
                ui.select(
                    load_opts, value=state["name"] or "", label="Load",
                ).classes("w-56").props("dense outlined").on_value_change(
                    lambda e: _load(e.value or None)
                ).tooltip(
                    "Load a previously saved template's values into the form "
                    "below for editing, or leave on \"New Template\" to build one "
                    "from scratch."
                )
                name_input = ui.input(
                    "Template name", value=state["name"] or "",
                ).classes("w-56").props("dense outlined")

            ui.label("Strategy").classes("text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1")
            with ui.grid(columns=2).classes("w-full gap-3 mb-2"):
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["tg_cmd_enabled"] = ui.switch(
                        "TG CMD", value=bool(live["tg_cmd_enabled"]),
                    ).classes("text-sm")
                    ui.label(
                        "Logic Keywords triggers (CLOSE ALL / RISK FREE-BE / TP HIT) "
                        "apply to trades opened under this template."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Mode").classes("text-sm")
                    fields["mode"] = ui.select(
                        {c: c.title() for c in et.MODE_CHOICES}, value=live["mode"],
                    ).classes("w-full").props("dense outlined").tooltip(
                        "Single: one entry per signal. Grid: stages multiple "
                        "entries step pts apart to average into the position."
                    )
                    fields["grid_step_pts"] = ui.number(
                        "Grid step (pt)", value=float(live["grid_step_pts"]), step=1.0,
                    ).classes("w-full mt-1").props("dense outlined")
                    fields["grid_legs"] = ui.number(
                        "Grid legs", value=int(live["grid_legs"]), step=1, min=2, max=10,
                    ).classes("w-full mt-1").props("dense outlined")
                    ui.label(
                        "Single: one entry per signal. Grid: stages multiple entries "
                        "step pts apart to average into the position."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("TP/SL").classes("text-sm")
                    fields["tpsl_mode"] = ui.select(
                        {c: c.title() for c in et.TPSL_MODE_CHOICES}, value=live["tpsl_mode"],
                    ).classes("w-full").props("dense outlined").tooltip(
                        "Off: no target, rides with no exit. On: real broker-side "
                        "SL/TP. Stealth: tracked internally, closes at market when "
                        "hit -- never written to the order ticket."
                    )
                    ui.label(
                        "Off: no target, rides with no exit. On: real broker-side "
                        "SL/TP. Stealth: tracked internally, closes at market when "
                        "hit -- never written to the order ticket."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Anchor").classes("text-sm")
                    fields["anchor"] = ui.select(
                        {c: c.title() for c in et.ANCHOR_CHOICES}, value=live["anchor"],
                    ).classes("w-full").props("dense outlined").tooltip(
                        "Unified: every leg trails/BEs off the original entry "
                        "price. Distributed: each leg manages its own reference "
                        "independently."
                    )
                    ui.label(
                        "Unified: every leg trails/BEs off the original entry price. "
                        "Distributed: each leg manages its own reference independently."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Trail").classes("text-sm")
                    fields["trail_mode"] = ui.select(
                        {c: c.title() for c in et.TRAIL_MODE_CHOICES}, value=live["trail_mode"],
                    ).classes("w-full").props("dense outlined").tooltip(
                        "Candle: trails to recent candle highs/lows. Step: fixed-"
                        "point trailing. Fractal: trails to swing-pivot fractals. "
                        "TP: trails up to each TP price as it's hit."
                    )
                    ui.label(
                        "Candle: trails to recent candle highs/lows. Step: fixed-"
                        "point trailing. Fractal: trails to swing-pivot fractals. "
                        "TP: trails up to each TP price as it's hit."
                    ).classes("text-xs text-gray-500 mt-1")

            ui.label("Triggers").classes("text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1")
            with ui.grid(columns=2).classes("w-full gap-3 mb-2"):
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("BE Mode").classes("text-sm")
                    fields["be_mode"] = ui.select(
                        {c: c.replace("_", " ").title() for c in et.BE_MODE_CHOICES},
                        value=live["be_mode"],
                    ).classes("w-full").props("dense outlined").tooltip(
                        "Entry: SL moves exactly to entry price. Entry+Buffer: "
                        "locks in buffer pts of profit instead of dead-even."
                    )
                    fields["be_buffer_pts"] = ui.number(
                        "BE buffer (pt)", value=float(live["be_buffer_pts"]), step=0.5,
                    ).classes("w-full mt-1").props("dense outlined")
                    ui.label(
                        "Entry: SL moves exactly to entry price. Entry+Buffer: locks "
                        "in buffer pts of profit instead of dead-even."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["be_trigger"] = ui.number(
                        "BE Trigger (TP#)", value=int(live["be_trigger"]), step=1, min=1, max=8,
                    ).classes("w-full").props("dense outlined")
                    ui.label(
                        "Which TP level arms the breakeven move."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["cancel_pending"] = ui.switch(
                        "Cancel Pending", value=bool(live["cancel_pending"]),
                    ).classes("text-sm")
                    ui.label(
                        "When one leg of a multi-order signal fills, cancel the "
                        "other still-resting pending legs."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["sig_guard"] = ui.switch(
                        "Sig Guard", value=bool(live["sig_guard"]),
                    ).classes("text-sm")
                    ui.label(
                        "Block a new template-managed trade for the same channel/"
                        "direction while one is already open."
                    ).classes("text-xs text-gray-500 mt-1")

            with ui.row().classes("items-center gap-2 mb-2 mt-1"):
                ui.label("Anchor TP").classes(
                    "text-xs font-semibold text-gray-400 uppercase tracking-wider"
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Per-TP-level pips + close %. Pips are only used as a fallback "
                    "for any TP level the raw signal itself didn't supply (entry ± "
                    "N pips) -- a level the signal DID state always keeps that "
                    "price. % always comes from here regardless, since a signal "
                    "states TP prices but never how much to close at each one. "
                    "0 = level unused."
                )
            with ui.card().classes("bg-gray-900 p-3 rounded-lg w-full mb-2"):
                with ui.grid(columns=9).classes("w-full gap-2 items-center"):
                    ui.label("")
                    for n in range(1, 9):
                        ui.label(f"TP{n}").classes("text-xs font-semibold text-yellow-300 text-center")
                    ui.label("pips").classes("text-xs text-gray-500")
                    for n in range(1, 9):
                        fields[f"tp{n}_pips"] = ui.number(
                            value=float(live[f"tp{n}_pips"]), step=1.0, min=0,
                        ).classes("w-full").props("dense outlined")
                    ui.label("%").classes("text-xs text-gray-500")
                    for n in range(1, 9):
                        fields[f"tp{n}_pct"] = ui.number(
                            value=float(live[f"tp{n}_pct"]), step=1.0, min=0, max=100,
                        ).classes("w-full").props("dense outlined")

            with ui.row().classes("gap-2 mt-1"):
                ui.button(
                    "Save Template", on_click=lambda: _save(name_input),
                ).classes("text-xs bg-green-800 text-white").props("dense")
                if state["name"]:
                    ui.button(
                        "Delete", on_click=lambda: _delete(state["name"]),
                    ).classes("text-xs bg-red-900 text-white").props("dense")
                ui.button(
                    "New", on_click=lambda: _load(None),
                ).classes("text-xs").props("dense outline")

    def _save(name_input) -> None:
        name = (name_input.value or "").strip()
        if not name:
            ui.notify("Enter a template name first", type="warning")
            return
        try:
            et.save_ea_template(name, _current_values())
            ui.notify(f"Saved template '{name}'", type="positive")
            state["name"] = name
            _draw_body()
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    def _delete(name: str) -> None:
        et.delete_ea_template(name)
        ui.notify(f"Deleted template '{name}'", type="info")
        _load(None)

    _draw_body()
