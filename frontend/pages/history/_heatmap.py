"""The day/hour performance heat map and its AI commentary."""

from nicegui import ui

from backend.src.controllers import history_controller as history_ctl
from frontend.components.empty_state import render_empty_state

import logging

_log = logging.getLogger(__name__)


def _render_heatmap(engine):
    """Grid of average net P&L by UTC hour (columns) × weekday (rows).
    Surfaces hours we consistently lose so they can be avoided.
    AI analysis panel on the right summarises wins/losses and likely causes."""
    _DOW   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    period = {"days": 90}
    # container and _analysis_body are assigned inside the layout below;
    # _draw() and _refresh_heatmap_analysis() reference them via closure (late binding).
    _refs: dict = {}

    def _draw():
        container = _refs["container"]
        container.clear()
        grid = history_ctl.get_hourly_pnl_grid(period["days"])
        with container:
            ui.label(
                "Average net P&L by UTC hour × weekday — deep red = hours that consistently "
                "lose (candidates to stop trading), green = consistent winners."
            ).classes("text-xs text-gray-400 mb-1")
            if not grid:
                render_empty_state("closed_trades", compact=True)
                return

            max_abs = max((abs(c["avg"]) for c in grid.values()), default=1.0) or 1.0

            with ui.grid(columns=25).classes("gap-0.5 w-full"):
                ui.label("").classes("text-xs")
                for h in range(24):
                    ui.label(f"{h:02d}").classes("text-center text-[10px] text-gray-500")
                for di, dname in enumerate(_DOW):
                    ui.label(dname).classes("text-xs text-gray-400 font-semibold self-center")
                    for h in range(24):
                        cell = grid.get((di, h))
                        if not cell or cell["n"] == 0:
                            ui.element("div").style(
                                "min-height:24px;background:#0f1117;border-radius:3px"
                            )
                            continue
                        avg = cell["avg"]
                        intensity = 0.18 + 0.62 * min(1.0, abs(avg) / max_abs)
                        bg = (f"rgba(34,197,94,{intensity})" if avg >= 0
                              else f"rgba(239,68,68,{intensity})")
                        el = ui.element("div").classes("cursor-help").style(
                            f"min-height:24px;background:{bg};border-radius:3px"
                        )
                        el.tooltip(
                            f"{dname} {h:02d}:00 UTC — avg ${avg:+.2f} · "
                            f"{cell['n']} trade{'s' if cell['n'] != 1 else ''} · "
                            f"total ${cell['pnl']:+.2f}"
                        )

            sess = {"london": [0.0, 0], "overlap": [0.0, 0], "ny": [0.0, 0], "asian": [0.0, 0]}
            for (di, h), c in grid.items():
                s = history_ctl.session_for_hour(h)
                sess[s][0] += c["pnl"]
                sess[s][1] += c["n"]
            ui.separator().classes("my-2")
            ui.label("By session").classes("text-xs font-semibold text-gray-300 uppercase tracking-wider")
            with ui.row().classes("gap-2 flex-wrap"):
                for name, (tot, n) in sess.items():
                    col = "text-green-400" if tot >= 0 else "text-red-400"
                    with ui.card().classes("bg-gray-800 rounded p-2 min-w-28"):
                        ui.label(name.upper()).classes("text-xs text-gray-400")
                        ui.label(f"${tot:+.2f}").classes(f"text-sm font-bold {col}")
                        ui.label(f"{n} trades").classes("text-xs text-gray-500")

    def _refresh_heatmap_analysis(force: bool = False) -> None:
        import asyncio as _asyncio
        import json as _json
        from datetime import datetime as _dt
        from backend.src.controllers.settings_controller import load_config as _cfg_load
        _analysis_body = _refs["analysis_body"]

        stored = history_ctl.get_app_config("heatmap_analysis_cache")
        if stored and not force:
            try:
                obj = _json.loads(stored)
                age_h = (_dt.now().timestamp() - obj.get("ts", 0)) / 3600
                if age_h < 20:
                    _render_heatmap_analysis(obj.get("result", {}), obj.get("ts", 0))
                    return
            except Exception as e:
                _log.debug("[history] cached heatmap render failed: %s", e)

        cfg = _cfg_load()

        _analysis_body.clear()
        with _analysis_body:
            with ui.row().classes("items-center gap-2"):
                ui.spinner("dots", size="sm", color="yellow")
                ui.label("Analysing with AI...").classes("text-xs text-gray-400")

        async def _run_analysis():
            _body = _refs["analysis_body"]
            grid  = history_ctl.get_hourly_pnl_grid(period["days"])
            if not grid:
                _body.clear()
                with _body:
                    ui.label("No trade data for analysis.").classes("text-xs text-gray-500 italic")
                return

            sess_totals: dict = {}
            best_hours:  list = []
            worst_hours: list = []
            for (di, h), c in grid.items():
                s = history_ctl.session_for_hour(h)
                if s not in sess_totals:
                    sess_totals[s] = {"pnl": 0.0, "n": 0, "wins": 0}
                sess_totals[s]["pnl"] += c["pnl"]
                sess_totals[s]["n"]   += c["n"]
                if c["avg"] > 0:
                    sess_totals[s]["wins"] += c["n"]
                if c["n"] >= 3:
                    best_hours.append((c["avg"], di, h, c["n"]))
                    worst_hours.append((c["avg"], di, h, c["n"]))

            best_hours  = sorted(best_hours,  key=lambda x: -x[0])[:5]
            worst_hours = sorted(worst_hours, key=lambda x:  x[0])[:5]

            lines = [
                f"XAUUSD Trading Performance Heatmap Analysis ({period['days']} day window)",
                "",
                "SESSION TOTALS:",
            ]
            for s, d in sorted(sess_totals.items()):
                wr = round(d["wins"] / d["n"] * 100) if d["n"] else 0
                lines.append(f"  {s.upper()}: {d['n']} trades, ${d['pnl']:+.2f} P&L, ~{wr}% WR")
            lines += ["", "BEST UTC HOUR × WEEKDAY CELLS (≥3 trades):"]
            for avg, di, h, n in best_hours:
                lines.append(f"  {_DOW[di]} {h:02d}:00 UTC — avg ${avg:+.2f} ({n} trades)")
            lines += ["", "WORST UTC HOUR × WEEKDAY CELLS (≥3 trades):"]
            for avg, di, h, n in worst_hours:
                lines.append(f"  {_DOW[di]} {h:02d}:00 UTC — avg ${avg:+.2f} ({n} trades)")

            prompt = "\n".join(lines) + (
                "\n\nTASK: Analyse where this XAUUSD trader wins and loses on the heatmap. "
                "Identify patterns by session, day-of-week, and time-of-day. "
                "Suggest likely CAUSES for losses: is it the strategy, signal quality, market conditions "
                "(e.g. spread widening, low liquidity, volatility spikes), or execution/latency? "
                "For winning cells, explain what conditions favour success. "
                "Give 3-5 concrete, actionable recommendations. "
                "Plain text only, no JSON, under 400 words."
            )
            try:
                from backend.src.controllers import ai_controller as _aip
                result_text = await _aip.complete(
                    cfg,
                    "You are a professional XAUUSD trading analyst. "
                    "Analyse a performance heatmap (average P&L by UTC hour and weekday). "
                    "Be direct and practical. Focus on WHY patterns exist, not just WHAT they are.",
                    prompt,
                    max_tokens=1024,
                    timeout=60,
                )
                import json as _json2
                from datetime import datetime as _dt2
                ts = _dt2.now().timestamp()
                history_ctl.set_app_config("heatmap_analysis_cache", _json2.dumps({
                    "ts": ts, "result": {"text": result_text, "period": period["days"]}
                }))
                _render_heatmap_analysis({"text": result_text, "period": period["days"]}, ts)
            except Exception as exc:
                _body.clear()
                with _body:
                    ui.label(f"Analysis error: {exc}").classes("text-xs text-red-400")

        _asyncio.create_task(_run_analysis())

    def _render_heatmap_analysis(result: dict, ts: float) -> None:
        from datetime import datetime as _dt
        _body = _refs["analysis_body"]
        _body.clear()
        with _body:
            if ts:
                try:
                    ui.label(_dt.fromtimestamp(ts).strftime("Updated %d %b %H:%M")).classes(
                        "text-xs text-gray-600 mb-1"
                    )
                except Exception as e:
                    _log.debug("[history] heatmap analysis render failed: %s", e)
            text = result.get("text", "No analysis available.")
            for para in text.split("\n\n"):
                para = para.strip()
                if not para:
                    continue
                # Bold headings: lines that are all-caps or end with a colon
                if (para.endswith(":") or (para == para.upper() and len(para) < 60)):
                    ui.label(para.rstrip(":")).classes("text-xs font-bold text-yellow-300 mt-2")
                else:
                    ui.label(para).classes("text-xs text-gray-300 leading-relaxed")

    # ── Controls row ──────────────────────────────────────────────────────────
    with ui.row().classes("items-center gap-3 mb-2 flex-wrap"):
        ui.label("Performance Heat Map").classes("text-base font-bold text-yellow-300")
        _sel = ui.select(
            {1: "1 day", 7: "7 days", 14: "14 days",
             30: "30 days", 90: "90 days", 180: "180 days", 365: "1 year"},
            value=90, label="Period",
        ).classes("w-32")

        def _on_period(e):
            period["days"] = int(_sel.value)
            _draw()
            _refresh_heatmap_analysis(force=False)
        _sel.on("update:model-value", _on_period)

        ui.button(icon="refresh", on_click=_draw).classes(
            "bg-blue-700 text-white text-xs px-2 py-1"
        ).tooltip("Refresh heatmap")
        ui.button("AI Analysis", icon="smart_toy",
                  on_click=lambda: _refresh_heatmap_analysis(force=True)).classes(
            "bg-yellow-700 text-white text-xs px-2 py-1"
        ).tooltip("Run AI analysis of heatmap wins/losses (cached daily at 8am)")

    # ── Two-column layout: heatmap left, AI analysis right ────────────────────
    with ui.row().classes("w-full gap-4 items-start flex-wrap"):
        with ui.column().classes("flex-1 min-w-0"):
            _refs["container"] = ui.column().classes("w-full gap-2")

        with ui.card().classes("bg-gray-800 p-4 rounded-lg").style("width:360px; min-width:260px"):
            ui.label("AI Heatmap Analysis").classes("text-sm font-bold text-yellow-300 mb-1")
            _refs["analysis_body"] = ui.column().classes("w-full gap-2")
            with _refs["analysis_body"]:
                ui.label("Click 'AI Analysis' or wait for the daily 8am refresh.").classes(
                    "text-xs text-gray-500 italic"
                )

    # ── Initial render ────────────────────────────────────────────────────────
    _draw()
    _refresh_heatmap_analysis(force=False)

    def _daily_8am_check():
        from datetime import datetime as _dt
        now = _dt.now()
        if now.hour == 8 and now.minute < 2:
            _refresh_heatmap_analysis(force=True)
    ui.timer(60, _daily_8am_check)
