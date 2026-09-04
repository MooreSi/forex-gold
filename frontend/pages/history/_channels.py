"""The channel scorecard: per-channel performance and pause/resume."""
import asyncio

from nicegui import ui

from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers import telegram_controller as telegram_alerts
from frontend.components.empty_state import render_empty_state


def _render_channels(engine):
    """Per-channel performance with rolling adaptive lot multiplier and pause control."""
    period    = {"days": 30}
    container = ui.column().classes("w-full gap-2")

    def _draw():
        container.clear()
        newly_paused = history_ctl.recompute_channel_performance(period["days"])
        for _src in newly_paused:
            asyncio.create_task(telegram_alerts.send_message(
                f"Channel auto-paused: *{_src}*\n"
                "Profit factor dropped below 0.8 over the rolling 30-day window. "
                "The channel will not open new trades until performance recovers or you re-enable it manually.",
            ))
        scorecard = history_ctl.get_channel_scorecard(period["days"])
        perfmap   = history_ctl.get_channel_performance_map()
        with container:
            ui.label(
                "Rolling performance by signal source. The lot multiplier scales position "
                "size on risk-based entries; paused channels are blocked from opening trades. "
                "Profit factor < 0.8 over 8+ trades auto-pauses; manual pause overrides."
            ).classes("text-xs text-gray-400 mb-2")
            if not scorecard:
                render_empty_state("closed_trades", compact=True)
                return
            for r in scorecard:
                src    = r["source"]
                pm     = perfmap.get(src, {})
                mult   = pm.get("lot_mult", 1.0)
                paused = pm.get("paused", False)
                wr     = r["win_rate"]
                wr_col = "text-green-400" if wr >= 55 else "text-yellow-400" if wr >= 45 else "text-red-400"
                pnl_col = "text-green-400" if r["net_pnl"] >= 0 else "text-red-400"
                border  = "#7f1d1d" if paused else ("#16532c" if r["net_pnl"] >= 0 else "#3f3f46")
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-3").style(
                    f"border-left:3px solid {border}"
                ):
                    with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
                        with ui.column().classes("gap-0 min-w-48"):
                            ui.label(src).classes("text-sm font-semibold text-gray-100")
                            ui.label(
                                f"{r['trades']} trades · {r['wins']}W/{r['losses']}L"
                            ).classes("text-xs text-gray-500")
                        with ui.row().classes("gap-4 items-center flex-wrap"):
                            with ui.column().classes("gap-0 items-center"):
                                ui.label(f"{wr:.0f}%").classes(f"text-sm font-bold {wr_col}")
                                ui.label("win rate").classes("text-[10px] text-gray-500")
                            with ui.column().classes("gap-0 items-center"):
                                ui.label(f"{r['avg_pts']:+.2f}").classes("text-sm font-mono text-gray-200")
                                ui.label("avg pts").classes("text-[10px] text-gray-500")
                            with ui.column().classes("gap-0 items-center"):
                                ui.label(f"{r['payoff_rr']:.2f}").classes("text-sm font-mono text-gray-200")
                                ui.label("payoff R:R").classes("text-[10px] text-gray-500")
                            with ui.column().classes("gap-0 items-center"):
                                ui.label(f"${r['net_pnl']:+.2f}").classes(f"text-sm font-bold font-mono {pnl_col}")
                                ui.label("net P&L").classes("text-[10px] text-gray-500")
                            with ui.column().classes("gap-0 items-center"):
                                ui.label(f"{mult:.1f}x").classes("text-sm font-bold text-blue-300")
                                ui.label("lot mult").classes("text-[10px] text-gray-500")
                            _btn_txt = "Resume" if paused else "Pause"
                            _btn_col = "bg-green-700" if paused else "bg-red-800"

                            def _toggle(_=None, s=src, p=paused):
                                history_ctl.set_channel_paused(s, not p)
                                ui.notify(
                                    f"Channel '{s}' {'resumed' if p else 'paused'}",
                                    type="info" if p else "warning",
                                )
                                _draw()
                            ui.button(_btn_txt, on_click=_toggle).classes(
                                f"{_btn_col} text-white text-xs px-3 py-1"
                            )
                    # Session split
                    ss = r["sessions"]
                    with ui.row().classes("gap-3 mt-1 flex-wrap"):
                        for sname in ("london", "overlap", "ny", "asian"):
                            v = ss.get(sname, 0.0)
                            c = "text-green-400" if v >= 0 else "text-red-400"
                            ui.label(f"{sname}: ").classes("text-[11px] text-gray-500") \
                                .style("display:inline")
                            ui.label(f"${v:+.0f}").classes(f"text-[11px] font-mono {c}")

    with ui.row().classes("items-center gap-3 mb-2"):
        ui.label("Channel Scorecard").classes("text-base font-bold text-yellow-300")
        _sel = ui.select(
            {1: "1 day", 7: "7 days", 30: "30 days", 90: "90 days"},
            value=30, label="Window",
        ).classes("w-32")

        def _on_period(e):
            period["days"] = int(_sel.value)
            _draw()
        _sel.on("update:model-value", _on_period)
        ui.button(icon="refresh", on_click=_draw).classes(
            "bg-blue-700 text-white text-xs px-2 py-1"
        )
    _draw()
