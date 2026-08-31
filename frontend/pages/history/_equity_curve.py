"""The equity curve."""
import asyncio

from nicegui import ui

from backend.src.controllers import settings_controller as cfg_module
from backend.src.controllers import history_controller as history_ctl

import logging

_log = logging.getLogger(__name__)


def _render_equity_curve(engine):
    with ui.card().classes("w-full bg-gray-800 p-0 rounded-lg mb-4 overflow-hidden"):
        with ui.row().classes("items-center justify-between px-4 pt-3 pb-2"):
            ui.label("Equity Curve").classes("font-semibold text-yellow-300")
            env_lbl = ui.label("").classes("text-xs text-gray-500")
        chart = ui.echart({
            "backgroundColor": "transparent",
            "xAxis": {
                "type": "category", "data": [],
                "axisLabel": {"color": "#e5e7eb", "fontSize": 11,
                              "rotate": 30, "interval": 8},
                "splitLine": {"show": False},
            },
            "yAxis": {
                "type": "value",
                "scale": True,
                "position": "right",
                "axisLabel": {"color": "#d1d5db", "fontSize": 11},
                "splitLine": {"lineStyle": {"color": "#1f2937", "type": "dashed"}},
            },
            "series": [{
                "name": "Equity ($)", "type": "line", "data": [],
                "smooth": True,
                "showSymbol": False,
                "lineStyle": {"color": "#00CC88", "width": 2},
                "areaStyle": {
                    "color": {
                        "type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
                        "colorStops": [
                            {"offset": 0,   "color": "rgba(0,204,136,0.25)"},
                            {"offset": 1,   "color": "rgba(0,204,136,0.02)"},
                        ],
                    }
                },
                "markLine": {
                    "silent": True,
                    "symbol": ["none", "none"],
                    "data": [],   # starting balance reference line added dynamically
                },
            }],
            "tooltip": {
                "trigger": "axis",
                "backgroundColor": "rgba(15,17,23,0.92)",
                "borderColor": "#374151",
                "textStyle": {"color": "#e5e7eb", "fontSize": 11},
                "formatter": "Equity: ${c}",
            },
            "grid": {"left": 10, "right": 60, "top": 15, "bottom": 62},
            "dataZoom": [
                {"type": "inside", "start": 0, "end": 100},
                {"type": "slider", "bottom": 4, "height": 18,
                 "start": 0, "end": 100,
                 "borderColor": "#1f2937",
                 "fillerColor": "rgba(0,204,136,0.06)",
                 "handleStyle": {"color": "#374151"},
                 "textStyle": {"color": "#6b7280", "fontSize": 9}},
            ],
        }).classes("w-full").style("height:280px")

        async def refresh_chart():
            try:
                env = cfg_module.get_config("account_env", "demo")
                starting = float(cfg_module.get_config("starting_balance", 1000.0))
                env_label = "⚡ LIVE" if env == "live" else "DEMO"
                env_lbl.text = env_label

                rows: list[tuple[float, float]] = []  # (close_timestamp, pnl)

                # Try MT5 deal history first — raw profit+swap+fee, deliberately NOT
                # run through _apply_fee()'s estimated-commission deduction: this
                # curve traces the account's actual equity/balance trajectory, so it
                # must sum to exactly (starting + real balance change) or it no
                # longer represents the real account. The Closed Trades table/
                # calendar use the fee-adjusted figure instead since their job is
                # showing realistic net-of-cost P&L per trade, a different purpose.
                try:
                    deals = await engine._bridge.get_deal_history(365)
                    by_pos: dict[int, list] = {}
                    for d in deals:
                        pid = d.get("position_id")
                        if pid:  # excludes None and 0 (balance/deposit ops)
                            by_pos.setdefault(int(pid), []).append(d)

                    for _ticket, pos_deals in by_pos.items():
                        _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
                        close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
                        if not close_deal:
                            continue
                        close_ts = float(close_deal.get("time", 0))
                        if not close_ts:
                            continue
                        pnl = round(sum(
                            float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
                            for d in pos_deals
                        ), 2)
                        rows.append((close_ts, pnl))
                except Exception as e:
                    _log.debug("[history] equity-curve row build failed: %s", e)

                if not rows:
                    chart.options["xAxis"]["data"]     = []
                    chart.options["series"][0]["data"] = []
                    chart.update()
                    return

                rows.sort(key=lambda r: r[0])

                equity = starting
                x_data, y_data = [], []
                for ts, pnl in rows:
                    equity += pnl
                    x_data.append(history_ctl.format_broker_ts(ts))
                    y_data.append(round(equity, 2))

                chart.options["series"][0]["markLine"]["data"] = [{
                    "yAxis": starting,
                    "lineStyle": {"color": "rgba(251,191,36,0.4)", "type": "dashed", "width": 1},
                    "label": {
                        "show": True, "formatter": f"Start ${starting:,.0f}",
                        "color": "#fbbf24", "fontSize": 9, "position": "end",
                    },
                }]
                chart.options["xAxis"]["data"]     = x_data
                chart.options["series"][0]["data"] = y_data
                chart.update()
            except Exception as e:
                _log.debug("[history] equity-curve chart update failed: %s", e)

        ui.timer(15.0, refresh_chart)
        asyncio.ensure_future(refresh_chart())
