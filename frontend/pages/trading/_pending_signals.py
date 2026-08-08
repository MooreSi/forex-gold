"""Pending signals awaiting activation or a zone re-entry."""
import asyncio
from nicegui import ui
from backend.src.controllers import trading_controller as trading_ctl

# Sibling sections of this page.
from ._shared import (
    _stat_cell,
    _uk,
)


def _render_pending_signals(engine):
    container = ui.column().classes("w-full gap-3")

    async def refresh():
        container.clear()
        sigs = await trading_ctl.get_signals(engine, status="pending")
        with container:
            if not sigs:
                ui.label(
                    "No pending signals. Use 'Limit Order' to create one, or signals from "
                    "Telegram that haven't been executed will appear here."
                ).classes("text-gray-500 text-sm italic p-4")
                return

            for s in sigs:
                signal_id = s["signal_id"]
                direction = s.get("direction", "?")
                source    = s.get("source_name", "Manual")

                btn_ref = [None]

                async def open_trade(sid=signal_id, _btn=btn_ref):
                    if _btn[0]:
                        _btn[0].props("loading=true disabled=true")
                    try:
                        result = await engine.open_trade_from_signal(sid)
                        ui.notify(
                            f"Trade opened @ {result['entry_price']}", type="positive"
                        )
                        await refresh()
                    except Exception as e:
                        ui.notify(str(e), type="negative")
                    finally:
                        if _btn[0]:
                            try:
                                _btn[0].props(remove="loading disabled")
                            except Exception:
                                pass

                def cancel_sig(sid=signal_id):
                    engine.cancel_signal(sid)
                    ui.notify("Signal cancelled", type="warning")
                    asyncio.create_task(refresh())

                with ui.card().classes("w-full max-w-2xl bg-gray-800 p-4 rounded-lg"):
                    with ui.row().classes("items-center justify-between mb-2"):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(direction, color="green" if direction == "BUY" else "red")
                            ui.label(f"XAUUSD — {source}").classes(
                                "text-sm font-semibold text-gray-200"
                            )
                        ui.label(_uk(s.get("created_at"))).classes("text-xs text-gray-500")

                    with ui.grid(columns=4).classes("w-full text-sm gap-2"):
                        _stat_cell(
                            "ENTRY RANGE",
                            f"{float(s['entry_low']):.2f} – {float(s['entry_high']):.2f}",
                        )
                        _stat_cell("SL", f"{float(s['stop_loss']):.2f}", "text-red-400")
                        tp_str = "  ".join(
                            f"TP{i}: {float(s[f'tp{i}']):.0f}"
                            for i in range(1, 9) if s.get(f"tp{i}")
                        )
                        _stat_cell("TPs", tp_str or "—", "text-green-400")
                        _stat_cell("SIGNAL ID", signal_id[:8])

                    if s.get("notes"):
                        ui.label(s["notes"]).classes("text-xs text-gray-500 mt-1")

                    # Claude commentary if available
                    commentary = s.get("claude_commentary")
                    if commentary and isinstance(commentary, dict) and commentary.get("summary"):
                        with ui.expansion("AI Commentary", icon="smart_toy").classes(
                            "w-full bg-gray-700 rounded mt-2 text-xs"
                        ):
                            ui.label(commentary["summary"]).classes("text-gray-300 text-xs p-2")

                    with ui.row().classes("gap-2 mt-3 flex-wrap"):
                        btn_ref[0] = ui.button("Open Trade Now", on_click=open_trade).classes(
                            "bg-green-700 text-white text-xs px-3 py-1"
                        )

                        # ── Edit signal dialog ─────────────────────────────────
                        with ui.dialog() as edit_dialog, ui.card().classes(
                            "bg-gray-800 p-5 rounded-lg w-full max-w-lg"
                        ):
                            ui.label("Edit Signal").classes(
                                "text-base font-semibold text-yellow-300 mb-3"
                            )
                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "BUY = expecting price to rise (long). SELL = expecting price to fall (short)."
                                    )
                                e_dir = ui.select(
                                    ["BUY", "SELL"], value=s.get("direction", "BUY"),
                                ).classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Entry Low").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "The lower price of your entry zone. For a single entry price, "
                                        "set both Low and High to the same value."
                                    )
                                e_el = ui.number(value=float(s.get("entry_low", 0)), format="%.2f").classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Entry High").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "The upper price of your entry zone. Must be >= Entry Low. "
                                        "For a single entry price, set both Low and High to the same value."
                                    )
                                e_eh = ui.number(value=float(s.get("entry_high", 0)), format="%.2f").classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Stop Loss").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "Price at which the trade closes at a loss. "
                                        "For BUY: place below entry. For SELL: place above entry."
                                    )
                                e_sl = ui.number(value=float(s.get("stop_loss", 0)), format="%.2f").classes("w-full")

                            with ui.expansion("Take Profit Levels", icon="expand_more").classes(
                                "w-full bg-gray-700 rounded mt-3"
                            ):
                                with ui.grid(columns=3).classes("gap-3 p-1"):
                                    _etp_defs = [
                                        ("TP1", "First target. Most strategies close a portion here and move SL to breakeven."),
                                        ("TP2", "Second target. Remaining position continues after TP1 is hit."),
                                        ("TP3", "Third target."),
                                        ("TP4", "Fourth target."),
                                        ("TP5", "Fifth target."),
                                        ("TP6", "Sixth target."),
                                        ("TP7", "Seventh target."),
                                        ("TP8", "Final target — Conservative strategy exits at TP7 (second-to-last); TP8 is headroom only."),
                                    ]
                                    _etp_keys = ["tp1","tp2","tp3","tp4","tp5","tp6","tp7","tp8"]
                                    _etp_inputs = []
                                    for (_elbl, _etip), _ekey in zip(_etp_defs, _etp_keys):
                                        with ui.column().classes("gap-0"):
                                            with ui.row().classes("items-center gap-0.5"):
                                                ui.label(_elbl).classes("text-xs text-gray-400 font-medium")
                                                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(_etip)
                                            _etp_inputs.append(
                                                ui.number(value=float(s.get(_ekey) or 0), format="%.2f").classes("w-full")
                                            )
                                    e_tp1, e_tp2, e_tp3, e_tp4, e_tp5, e_tp6, e_tp7, e_tp8 = _etp_inputs

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Notes (optional)").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "Free text notes about this signal — e.g. source channel, setup reason."
                                    )
                                e_notes = ui.input(value=s.get("notes", "") or "").classes("w-full")
                            e_result = ui.label("").classes("text-xs text-gray-400 mt-1")

                            async def save_edit(sid=signal_id):
                                try:
                                    updates = {
                                        "direction":  e_dir.value,
                                        "entry_low":  float(e_el.value or 0),
                                        "entry_high": float(e_eh.value or 0),
                                        "stop_loss":  float(e_sl.value or 0),
                                        "tp1": float(e_tp1.value) if e_tp1.value else None,
                                        "tp2": float(e_tp2.value) if e_tp2.value else None,
                                        "tp3": float(e_tp3.value) if e_tp3.value else None,
                                        "tp4": float(e_tp4.value) if e_tp4.value else None,
                                        "tp5": float(e_tp5.value) if e_tp5.value else None,
                                        "tp6": float(e_tp6.value) if e_tp6.value else None,
                                        "tp7": float(e_tp7.value) if e_tp7.value else None,
                                        "tp8": float(e_tp8.value) if e_tp8.value else None,
                                        "notes": e_notes.value or "",
                                    }
                                    result = await engine.update_signal(sid, updates)
                                    trade_note = ""
                                    if result.get("trade_updated"):
                                        trade_note = " — open trade SL/TP updated"
                                    e_result.text = f"Saved{trade_note}"
                                    e_result.classes(replace="text-xs text-green-400 mt-1")
                                    ui.notify(f"Signal updated{trade_note}", type="positive")
                                    edit_dialog.close()
                                    await refresh()
                                except Exception as ex:
                                    e_result.text = str(ex)
                                    e_result.classes(replace="text-xs text-red-400 mt-1")
                                    ui.notify(str(ex), type="negative")

                            with ui.row().classes("gap-2 mt-3"):
                                ui.button(
                                    "Save Changes",
                                    on_click=lambda: asyncio.create_task(save_edit()),
                                ).classes("bg-blue-700 text-white px-4 py-2")
                                ui.button("Cancel", on_click=edit_dialog.close).classes(
                                    "bg-gray-700 text-white px-4 py-2"
                                )

                        ui.button(
                            "Edit Signal", icon="edit", on_click=edit_dialog.open
                        ).classes("bg-gray-700 text-white text-xs px-3 py-1")

                        ui.button("Cancel Signal", on_click=cancel_sig).classes(
                            "bg-gray-600 text-white text-xs px-3 py-1"
                        )

    ui.timer(5.0, refresh)
    asyncio.ensure_future(refresh())
