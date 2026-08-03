"""Raw Telegram signals list."""
import asyncio
from nicegui import ui
from backend.src.controllers.trading import controller as trading_ctl

# Sibling sections of this page.
from ._shared import _uk


def _render_tg_signals(engine):
    container = ui.column().classes("w-full gap-1")

    async def refresh():
        container.clear()
        sigs = await trading_ctl.run_db(engine.get_tg_signals, 50)
        with container:
            if not sigs:
                ui.label("No Telegram signals detected yet.").classes(
                    "text-gray-500 italic p-4"
                )
                return

            # Header row
            with ui.row().classes(
                "w-full items-center gap-2 px-3 py-1 text-xs text-gray-500 font-semibold "
                "uppercase tracking-wider border-b border-gray-700"
            ):
                ui.label("Time").classes("w-24 shrink-0")
                ui.label("Channel").classes("w-32 shrink-0 truncate")
                ui.label("Dir").classes("w-14 shrink-0 text-center")
                ui.label("Entry").classes("w-24 shrink-0 text-right")
                ui.label("SL").classes("w-20 shrink-0 text-right")
                ui.label("TPs").classes("flex-1 min-w-0")
                ui.label("Status").classes("w-36 shrink-0 text-center")
                ui.label("").classes("w-36 shrink-0")   # Execute + Delete columns

            for s in sigs:
                sig_id    = s.get("id")
                direction = s.get("direction", "?")
                tp_str    = "  ".join(
                    f"TP{i}:{s.get(f'tp{i}'):.0f}"
                    for i in range(1, 9) if s.get(f"tp{i}")
                )
                dir_cls = "text-green-400" if direction == "BUY" else "text-red-400"
                status  = s.get("status", "?")

                async def delete_sig(row_id=sig_id):
                    try:
                        trading_ctl.delete_tg_signal_row(row_id)
                        await refresh()
                    except Exception as ex:
                        ui.notify(str(ex), type="negative")

                # Use the actual Telegram message send time; fall back to import time
                display_ts = s.get("message_ts") or s.get("parsed_at")
                status_color = {
                    "new":                  "blue",
                    "activated":            "green",
                    "historical":           "grey",
                    "pending":              "orange",
                    "instant_pending":      "amber",
                    "instant_activated":    "green",
                    "instant_failed":       "red",
                    "followup_applied":     "teal",
                    "unsupported_currency": "orange",
                }.get(status, "grey")

                with ui.row().classes(
                    "w-full items-center gap-2 px-3 py-1.5 text-xs "
                    "bg-gray-800 rounded hover:bg-gray-750"
                ).style("background:#1e2433"):
                    ui.label(_uk(display_ts)).classes(
                        "w-24 shrink-0 font-mono text-gray-400"
                    )
                    ui.label(
                        s.get("group_name") or s.get("group_id", "—")
                    ).classes("w-32 shrink-0 truncate text-gray-300")
                    ui.label(direction).classes(f"w-14 shrink-0 text-center font-bold {dir_cls}")
                    _entry_lo = s.get("entry_low")
                    _entry_hi = s.get("entry_high")
                    _sl_val   = s.get("stop_loss")
                    ui.label(
                        f"{float(_entry_lo):.0f}–{float(_entry_hi):.0f}"
                        if _entry_lo and _entry_hi else "—"
                    ).classes("w-24 shrink-0 text-right font-mono text-gray-200")
                    ui.label(
                        f"{float(_sl_val):.0f}" if _sl_val else "—"
                    ).classes("w-20 shrink-0 text-right font-mono text-red-400")
                    ui.label(tp_str or "—").classes(
                        "flex-1 min-w-0 font-mono text-green-400 truncate"
                    )
                    ui.badge(status, color=status_color).classes(
                        "w-36 px-2 shrink-0 text-center"
                    )
                    exec_btn_ref = [None]

                    async def execute_sig(row_id=sig_id, sig_data=s, _btn=exec_btn_ref):
                        if sig_data.get("status") == "unsupported_currency":
                            ui.notify("Cannot execute — signal is not XAUUSD", type="warning")
                            return
                        if _btn[0]:
                            _btn[0].props("loading=true disabled=true")
                        try:
                            new_sig = engine.create_signal(
                                source_name=sig_data.get("group_name") or "TG",
                                direction=sig_data.get("direction", "BUY"),
                                entry_low=float(sig_data.get("entry_low") or 0),
                                entry_high=float(sig_data.get("entry_high") or 0),
                                stop_loss=float(sig_data.get("stop_loss") or 0),
                                tp1=sig_data.get("tp1") or None,
                                tp2=sig_data.get("tp2") or None,
                                tp3=sig_data.get("tp3") or None,
                                tp4=sig_data.get("tp4") or None,
                                tp5=sig_data.get("tp5") or None,
                                tp6=sig_data.get("tp6") or None,
                                tp7=sig_data.get("tp7") or None,
                                tp8=sig_data.get("tp8") or None,
                            )
                            result = await engine.open_trade_from_signal(new_sig["signal_id"])
                            ui.notify(
                                f"Trade opened @ {result['entry_price']}", type="positive"
                            )
                            await refresh()
                        except Exception as ex:
                            ui.notify(str(ex), type="negative")
                        finally:
                            if _btn[0]:
                                try:
                                    _btn[0].props(remove="loading disabled")
                                except Exception:
                                    pass

                    exec_btn_ref[0] = ui.button(
                        "Execute", on_click=execute_sig,
                    ).classes(
                        "shrink-0 bg-green-800 text-green-300 text-xs px-2 py-0.5 "
                        "hover:bg-green-600"
                    ).props("dense flat")
                    ui.button(
                        "Delete", on_click=delete_sig,
                    ).classes(
                        "shrink-0 bg-red-900 text-red-300 text-xs px-2 py-0.5 "
                        "hover:bg-red-700"
                    ).props("dense flat")

    ui.timer(3.0, refresh)
    asyncio.ensure_future(refresh())
