"""
Chart page — live XAUUSD candlestick + RSI chart.

Architecture (matches the Hummingbot Vantage chart in style/data):
  - Candlestick with EMA 9 / EMA 21 / EMA 50 overlays
  - Bid and Ask mark lines
  - Entry mark lines from any open trades (SL/TP lines removed 2026-08-04 --
    they buried the price action; the numbers live on the trades panel)
  - Fair Value Gap zones, own timeframe selector (M1/M5/M15), independent
    of the chart timeframe
  - RSI 14 sub-chart with overbought/oversold zones
  - Fast refresh (3s): tick, last-candle live update, RSI update
  - Slow refresh (10s): full candle batch + all EMAs
"""

import asyncio
import logging
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Optional, Callable

from nicegui import ui

from backend.src.utils.models import STRATEGY_NAMES, STRATEGY_SCALE_OUT
from backend.src.controllers import chart_controller as chart_controller
from backend.src.controllers import sync_controller as sync_ctl
from backend.src.services.positions.core_indicators import ema_series, rsi_series
from backend.src.services.analytics.reporting import is_stuck_placeholder
from frontend.pages.trading import trade_source_label, trade_channel_label

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]
TF_MAP     = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
              "1H": "H1", "4H": "H4", "1D": "D1"}

# EMA periods and colours — matches Hummingbot chart
_EMAS = [
    (9,  "#FFD700"),   # gold
    (21, "#FF9900"),   # orange
    (50, "#00BFFF"),   # sky blue
]

_BG        = "#030712"
_BULL_COL  = "#00CC88"   # bright green (matches old app)
_BEAR_COL  = "#FF4444"   # bright red   (matches old app)
_RSI_COL   = "#FF9900"   # orange


# ── Indicator calculations ────────────────────────────────────────────────────

# EMA/RSI moved to core_indicators (2026-08-04) so the chart and the signal
# snapshot capture share one implementation instead of two that can drift.
_ema   = ema_series
_rsi14 = rsi_series


def _pnl_col(v: float) -> str:
    return "#4ade80" if v >= 0 else "#f87171"


def _untracked_position_node_label() -> str:
    """Which node opened an MT5 position this node's own DB has no record of.

    The Mac and VPS share one MT5 account, so a position missing from THIS
    node's vantage_simulated_trades table was opened by the other, currently-
    active node — the Local/Remote mutual-exclusion gate in engine.py's
    open_trade() guarantees only one node ever opens a given position, so
    this is a reliable inference, not a guess."""
    try:
        host, _, _ = sync_ctl.load_config()
        if host and chart_controller.get_active_trader() == sync_ctl.TRADER_REMOTE_VPS:
            return "Remote"
        return "Local"
    except Exception:
        return "Local"


# ── Chart render ──────────────────────────────────────────────────────────────

def render(get_engine: Callable):
    engine = get_engine()
    _state: dict = {
        "tf":      "15m",
        "count":   200,
        "candles": [],
        "tick":    None,
        "last_candle_refresh": 0.0,
        # FVG overlay (2026-08-04). Its timeframe is deliberately INDEPENDENT
        # of the chart's own: the signal provider marks gaps from one
        # timeframe's structure while you may be looking at another, and an
        # M15 gap is still the level price reacts to when you zoom into M1.
        # "off" draws nothing.
        "fvg_tf":   "15m",
        "fvgs":     [],
        "fvg_last": 0.0,
    }

    # ── Toolbar ───────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center bg-gray-900 px-3 py-1 gap-1 border-b border-gray-800 flex-wrap"
    ):
        ui.label("XAUUSD").classes("text-sm font-bold text-gray-200 pr-2 shrink-0")

        tf_btns: dict[str, ui.button] = {}
        for tf in TIMEFRAMES:
            def _click_tf(t=tf):
                for b in tf_btns.values():
                    b.style("background:#374151; color:#d1d5db")
                tf_btns[t].style("background:#ef4444; color:#fff")
                _state["tf"] = t
                _state["last_candle_refresh"] = 0.0
                asyncio.create_task(_refresh_candles())

            btn = ui.button(tf, on_click=_click_tf).style(
                "background:" + ("#ef4444" if tf == "15m" else "#374151") +
                "; color:" + ("#fff" if tf == "15m" else "#d1d5db") +
                "; font-size:11px; padding:2px 8px; border-radius:3px; min-height:0"
            )
            tf_btns[tf] = btn

        ui.element("div").classes("w-px bg-gray-700 mx-2 self-stretch")

        # Candle count slider (matches Hummingbot chart)
        ui.label("Candles:").classes("text-xs text-gray-500 shrink-0")
        count_slider = ui.slider(min=50, max=400, step=25, value=200).classes(
            "w-32 shrink-0"
        ).style("margin-top:0")
        count_slider.tooltip("Drag to change the number of candles shown")
        count_lbl = ui.label("200").classes("text-xs text-gray-400 w-7 shrink-0")

        def _on_count_change(e):
            v = int(e.value)
            count_lbl.text = str(v)
            _state["count"] = v
            _state["last_candle_refresh"] = 0.0
            asyncio.create_task(_refresh_candles())

        count_slider.on("update:model-value", _on_count_change)

        ui.element("div").classes("w-px bg-gray-700 mx-2 self-stretch")

        # FVG overlay timeframe. Separate from the chart timeframe above --
        # see _state["fvg_tf"].
        ui.label("FVG:").classes("text-xs text-gray-500 shrink-0")
        fvg_sel = ui.select(
            {"off": "OFF", "1m": "M1", "5m": "M5", "15m": "M15"},
            value=_state["fvg_tf"],
        ).props("dense outlined options-dense").classes("w-24 shrink-0").style(
            "font-size:11px; min-height:0"
        )
        fvg_sel.tooltip(
            "Draw Fair Value Gap zones from this timeframe's candles. Independent "
            "of the chart timeframe, so an M15 gap stays visible while you look at "
            "M1. Unfilled gaps are solid, filled ones faded, inverted ones outlined. "
            "Only the live zones are drawn (untested gaps, plus recent fills and "
            "inversions), not every historical imbalance."
        )

        def _on_fvg_tf(e):
            v = e.value if isinstance(e.value, str) else (e.value or {}).get("value")
            _state["fvg_tf"] = v or "off"
            _state["fvg_last"] = 0.0
            asyncio.create_task(_refresh_fvgs())

        fvg_sel.on("update:model-value", _on_fvg_tf)

        ui.element("div").classes("w-px bg-gray-700 mx-2 self-stretch")

        ohlc_lbl = ui.label("XAUUSD").classes("text-xs font-mono text-gray-400 shrink-0")

        # EMA legend chips
        with ui.row().classes("gap-1 ml-3 shrink-0"):
            for period, colour in _EMAS:
                ui.label(f"EMA {period}").classes("text-xs font-mono px-1 rounded").style(
                    f"color:{colour}; background:rgba(0,0,0,0.3)"
                )

        ui.space()
        update_lbl = ui.label("").classes("text-xs text-gray-600 shrink-0")

    # ── Main candlestick chart ─────────────────────────────────────────────────
    _series = [
        # 0: Candlestick
        {
            "name": "XAUUSD",
            "type": "candlestick",
            "data": [],
            "itemStyle": {
                "color":        _BULL_COL,
                "color0":       _BEAR_COL,
                "borderColor":  _BULL_COL,
                "borderColor0": _BEAR_COL,
                "borderWidth":  1,
            },
            "markLine": {
                "silent":    True,
                "symbol":    ["none", "none"],
                "animation": False,
                "data":      [],
            },
            # FVG zones. markArea with only yAxis bounds spans the full
            # chart width, which is what a price zone should do -- the gap
            # stays relevant to the right of where it formed, and that is
            # exactly how the provider's own screenshots draw them.
            "markArea": {
                "silent":    True,
                "animation": False,
                "data":      [],
            },
        },
    ]
    # EMA series 1, 2, 3
    for period, colour in _EMAS:
        _series.append({
            "name":       f"EMA {period}",
            "type":       "line",
            "data":       [],
            "smooth":     True,
            "showSymbol": False,
            "lineStyle":  {"color": colour, "width": 1.5},
            "z":          10,
        })

    with ui.card().classes("w-full p-0 rounded-none").style(
        f"background:{_BG}; border:none"
    ):
        chart = ui.echart({
            "backgroundColor": _BG,
            "animation": False,
            "grid": {"left": 10, "right": 90, "top": 30, "bottom": 72},
            "xAxis": {
                "type": "category",
                "data": [],
                "scale": True,
                "boundaryGap": True,
                "axisLine":  {"lineStyle": {"color": "#1f2937"}},
                "axisLabel": {
                    "color": "#e5e7eb", "fontSize": 11,
                    "rotate": 30,
                    "interval": 10,
                },
                "splitLine": {"show": False},
            },
            "yAxis": {
                "scale": True,
                "position": "right",
                "axisLine":  {"lineStyle": {"color": "#1f2937"}},
                "axisLabel": {"color": "#d1d5db", "fontSize": 11, "formatter": "{value}"},
                "splitLine": {"lineStyle": {"color": "#111827", "type": "dashed"}},
            },
            "toolbox": {
                "show": True,
                "feature": {
                    "saveAsImage": {"title": "Save", "pixelRatio": 2},
                    "dataZoom":    {"title": {"zoom": "Box Zoom", "back": "Undo Zoom"},
                                    "yAxisIndex": "none"},
                    "restore":     {"title": "Reset View"},
                },
                "orient": "horizontal",
                "right": 92,
                "top": 2,
                "iconStyle": {
                    "borderColor": "#4b5563",
                    "color":       "transparent",
                },
                "emphasis": {
                    "iconStyle": {"borderColor": "#d1d5db", "color": "transparent"},
                },
            },
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "cross"},
                "backgroundColor": "rgba(3,7,18,0.95)",
                "borderColor": "#374151",
                "textStyle": {"color": "#e5e7eb", "fontSize": 11},
            },
            "legend": {
                "show": True, "top": 4, "left": 4,
                "textStyle": {"color": "#6b7280", "fontSize": 10},
                "data": [f"EMA {p}" for p, _ in _EMAS],
            },
            "series": _series,
            "dataZoom": [
                {"type": "inside", "xAxisIndex": 0, "start": 60, "end": 100},
                {
                    "show": True, "xAxisIndex": 0, "type": "slider",
                    "bottom": 6, "height": 20,
                    "borderColor": "#1f2937",
                    "fillerColor": "rgba(255,215,0,0.06)",
                    "handleStyle": {"color": "#374151"},
                    "textStyle": {"color": "#6b7280", "fontSize": 9},
                },
            ],
        }).classes("w-full").style("height:440px")

    # ── RSI sub-chart ─────────────────────────────────────────────────────────
    with ui.card().classes("w-full p-0 rounded-none mt-px").style(
        f"background:{_BG}; border:none; border-top:1px solid #1f2937"
    ):
        rsi_chart = ui.echart({
            "backgroundColor": _BG,
            "animation": False,
            "grid": {"left": 10, "right": 90, "top": 8, "bottom": 58},
            "xAxis": {
                "type": "category",
                "data": [],
                "show": True,
                "axisLine":  {"lineStyle": {"color": "#1f2937"}},
                "axisLabel": {
                    "color": "#e5e7eb", "fontSize": 11,
                    "interval": 10,
                    "rotate": 30,
                },
                "splitLine": {"show": False},
                "axisTick":  {"show": False},
            },
            "yAxis": {
                "min": 0, "max": 100,
                "position": "right",
                "axisLine":  {"lineStyle": {"color": "#1f2937"}},
                "axisLabel": {"color": "#d1d5db", "fontSize": 10},
                "splitLine": {"show": False},
                "interval":  25,
            },
            "series": [
                {
                    "name":       "RSI 14",
                    "type":       "line",
                    "data":       [],
                    "smooth":     True,
                    "showSymbol": False,
                    "lineStyle":  {"color": _RSI_COL, "width": 1.5},
                    # Colour the area above 70 red, below 30 green
                    "areaStyle":  {
                        "color": {
                            "type":        "linear",
                            "x": 0, "y": 0, "x2": 0, "y2": 1,
                            "colorStops": [
                                {"offset": 0,    "color": "rgba(255,68,68,0.18)"},
                                {"offset": 0.3,  "color": "rgba(255,153,0,0.04)"},
                                {"offset": 0.7,  "color": "rgba(0,204,136,0.04)"},
                                {"offset": 1,    "color": "rgba(0,204,136,0.18)"},
                            ],
                        }
                    },
                    "markLine": {
                        "silent":    True,
                        "symbol":    ["none", "none"],
                        "animation": False,
                        "lineStyle": {"type": "dotted"},
                        "data": [
                            {
                                "yAxis": 70,
                                "lineStyle": {"color": "rgba(255,68,68,0.55)", "width": 1},
                                "label": {
                                    "show": True, "formatter": "OB 70",
                                    "color": "rgba(255,68,68,0.8)",
                                    "position": "end", "fontSize": 9,
                                },
                            },
                            {
                                "yAxis": 50,
                                "lineStyle": {"color": "rgba(150,150,150,0.25)", "width": 1},
                                "label": {
                                    "show": True, "formatter": "50",
                                    "color": "#6b7280",
                                    "position": "end", "fontSize": 9,
                                },
                            },
                            {
                                "yAxis": 30,
                                "lineStyle": {"color": "rgba(0,204,136,0.55)", "width": 1},
                                "label": {
                                    "show": True, "formatter": "OS 30",
                                    "color": "rgba(0,204,136,0.8)",
                                    "position": "end", "fontSize": 9,
                                },
                            },
                        ],
                    },
                },
            ],
            "tooltip": {
                "trigger": "axis",
                "backgroundColor": "rgba(3,7,18,0.92)",
                "borderColor": "#374151",
                "textStyle": {"color": "#e5e7eb", "fontSize": 10},
                "formatter": "RSI 14: {c}",
            },
        }).classes("w-full").style("height:160px")

    # RSI label row
    with ui.row().classes("w-full px-3 py-0.5 items-center gap-3").style(
        f"background:{_BG}"
    ):
        ui.label("RSI 14").classes("text-xs font-mono").style(f"color:{_RSI_COL}")
        ui.label("─── 70 Overbought").classes("text-xs font-mono").style(
            "color:rgba(255,68,68,0.7)"
        )
        ui.label("─── 30 Oversold").classes("text-xs font-mono").style(
            "color:rgba(0,204,136,0.7)"
        )
        ui.space()
        rsi_lbl = ui.label("").classes("text-xs font-mono text-gray-400 shrink-0")

    # ── Open trades panel ─────────────────────────────────────────────────────
    trades_panel = ui.column().classes("w-full gap-2 px-2 py-2")

    # ── Mark-line builder ─────────────────────────────────────────────────────

    def _build_mark_lines(trades: list[dict], tick) -> list[dict]:
        lines = []
        if tick:
            # Bid line (green)
            lines.append({
                "yAxis": tick.bid,
                "lineStyle": {"color": "rgba(0,204,136,0.7)", "width": 1, "type": "dotted"},
                "label": {
                    "show": True,
                    "formatter": f"Bid {tick.bid:.2f}",
                    "color": "#fff",
                    "backgroundColor": "rgba(0,204,136,0.75)",
                    "padding": [2, 4],
                    "fontSize": 10,
                    "position": "end",
                },
            })
            # Ask line (blue)
            lines.append({
                "yAxis": tick.ask,
                "lineStyle": {"color": "rgba(100,180,255,0.5)", "width": 1, "type": "dotted"},
                "label": {
                    "show": True,
                    "formatter": f"Ask {tick.ask:.2f}",
                    "color": "#fff",
                    "backgroundColor": "rgba(100,180,255,0.5)",
                    "padding": [2, 4],
                    "fontSize": 10,
                    "position": "end",
                },
            })

        # TP1-TP5 and SL lines for every active trade were removed here
        # (2026-08-04, explicit request). With several trades open at once
        # that was up to six dashed lines EACH, which buried the price
        # action and the FVG zones this chart now draws. Each trade's ENTRY
        # line is kept: it is one line per position and it is what locates
        # the trade on the chart at all. The live TP/SL numbers are still
        # on the trades panel beside the chart, so nothing is lost, it is
        # just no longer drawn over the candles.
        for t in trades:
            dir_col  = _BULL_COL if t.get("direction", "").upper() == "BUY" else _BEAR_COL
            entry_p  = float(t["entry_price"])
            lines.append({
                "yAxis": entry_p,
                "lineStyle": {"color": dir_col, "width": 1.5, "type": "solid"},
                "label": {
                    "show": True, "formatter": f"Entry {entry_p:.2f}",
                    "color": dir_col, "position": "end", "fontSize": 10,
                },
            })
        return lines

    # ── Trades panel renderer ─────────────────────────────────────────────────

    async def _refresh_trades_panel(trades: list[dict], tick, untracked: list[dict] | None = None) -> None:
        trades_panel.clear()
        with trades_panel:
            if not trades and not (untracked or []):
                ui.label("No open trades").classes("text-xs text-gray-500 italic px-2")
                return

            # ── Untracked MT5 positions ───────────────────────────────────────
            # "Untracked" means this node's own DB has no record — the trade was
            # opened by whichever node is the currently active trader. Before
            # falling back to a bare native-MT5-only card, check the sync
            # heartbeat: if the VPS opened this trade, its full detail
            # (strategy, TP ladder, channel, SL) is already mirrored to this
            # node every 3s via get_remote_open_position(), same source
            # trading.py's Active Trades tab already uses for this exact case.
            for pos in (untracked or []):
                ticket = pos.get("ticket", "—")
                remote = sync_ctl.get_remote_open_position(ticket)

                if remote:
                    direction  = (remote.get("direction") or pos.get("type", "BUY")).upper()
                    strategy   = remote.get("strategy", STRATEGY_SCALE_OUT)
                    strat_name = STRATEGY_NAMES.get(strategy, strategy)
                    triggered  = set(remote.get("triggered_tps") or [])
                    entry_p    = float(remote.get("entry_price") or pos.get("open_price") or 0)
                    current_p  = float(pos.get("current_price") or 0)
                    profit     = float(pos.get("profit") or 0)
                    sl_p       = float(remote.get("stop_loss") or 0) or None
                    lot_sz     = float(remote.get("lot_size") or pos.get("volume") or 0)
                    rem_sz     = float(remote.get("remaining_lots") or pos.get("volume") or 0)
                    closed_lots = round(lot_sz - rem_sz, 4)
                    lots_str   = f"{lot_sz:.2f} → {rem_sz:.2f}" if closed_lots > 0 else f"{lot_sz:.2f}"
                    lots_col   = "#fb923c" if closed_lots > 0 else "#e5e7eb"
                else:
                    direction  = pos.get("type", "BUY").upper()
                    strat_name = ""
                    triggered  = set()
                    entry_p    = float(pos.get("open_price") or 0)
                    current_p  = float(pos.get("current_price") or 0)
                    profit     = float(pos.get("profit") or 0)
                    sl_p       = float(pos.get("sl") or 0) or None
                    lots_str   = f"{float(pos.get('volume') or 0):.2f}"
                    lots_col   = "#e5e7eb"

                dir_col = _BULL_COL if direction == "BUY" else _BEAR_COL
                p_col   = _pnl_col(profit)
                node_lbl = "Remote" if remote else _untracked_position_node_label()

                elapsed = ""
                try:
                    _ot = (remote.get("open_time") if remote else None) or pos.get("open_time") or 0
                    secs = int(datetime.now(timezone.utc).timestamp() - float(_ot))
                    elapsed = (f"{secs // 60}m {secs % 60}s"
                               if secs < 3600 else
                               f"{secs // 3600}h {(secs % 3600) // 60}m")
                except Exception:
                    pass

                with ui.card().classes("w-full bg-gray-800 rounded-lg p-0 overflow-hidden mb-2"):
                    with ui.row().classes(
                        "w-full px-4 py-2 items-center gap-3"
                    ).style("background:#1a2236; border-bottom:1px solid #1f2937"):
                        ui.element("div").classes("w-1 h-5 rounded shrink-0").style(
                            f"background:{dir_col}"
                        )
                        ui.label(f"{direction} XAUUSD").classes(
                            "text-sm font-bold"
                        ).style(f"color:{dir_col}")
                        ui.label(f"#{ticket}").classes("text-xs text-gray-500")
                        if strat_name:
                            ui.label(strat_name).classes("text-xs text-gray-500")
                        if remote:
                            src_lbl = trade_source_label(remote.get("tg_source", ""))
                            ui.badge(src_lbl, color="purple").classes("text-xs")
                            ch = trade_channel_label(remote.get("tg_source", ""))
                            if ch and ch != src_lbl:
                                ui.badge(ch, color="indigo").classes("text-xs")
                        ui.space()
                        ui.badge(node_lbl, color="blue" if remote else "purple").classes("text-xs")
                        if remote and remote.get("sl_moved_to_be"):
                            ui.label("Breakeven").classes(
                                "text-xs px-2 py-0.5 rounded font-bold text-green-400"
                            ).style("background:rgba(34,197,94,0.12)").tooltip(
                                "Stop Loss has been moved to entry price — "
                                "this trade cannot lose money from the original risk."
                            )
                        ui.label(elapsed).classes("text-xs text-gray-500")

                    with ui.grid(columns=6).classes("w-full px-4 py-3 gap-x-4 gap-y-1"):
                        for label, val, col in [
                            ("ENTRY",      f"${entry_p:.2f}",                       "#e5e7eb"),
                            ("CURRENT",    f"${current_p:.2f}" if current_p else "—", "#e5e7eb"),
                            ("LOTS",       lots_str,                                lots_col),
                            ("SL",         f"${sl_p:.2f}" if sl_p else "—",         "#f87171"),
                            ("UNREAL P&L", f"${profit:+.2f}",                       p_col),
                            ("TPs",        "",                                      "#4ade80"),
                        ]:
                            with ui.column().classes("gap-0"):
                                ui.label(label).classes(
                                    "text-xs text-gray-500 tracking-wider"
                                )
                                if label == "TPs":
                                    with ui.row().classes("gap-1 flex-wrap"):
                                        if remote:
                                            has_any_tp = False
                                            for n in range(1, 9):
                                                tp_val = remote.get(f"tp{n}")
                                                if not tp_val:
                                                    continue
                                                has_any_tp = True
                                                hit = n in triggered
                                                bg_col = "#22c55e" if hit else "#374151"
                                                ui.label(f"TP{n}").classes(
                                                    "text-xs px-1.5 py-0.5 rounded "
                                                    "font-semibold text-white"
                                                ).style(f"background:{bg_col}").tooltip(f"${float(tp_val):.2f}")
                                            if not has_any_tp:
                                                ui.label("—").classes("text-sm font-mono").style(f"color:{col}")
                                        else:
                                            tp_p = float(pos.get("tp") or 0) or None
                                            if tp_p:
                                                ui.label(f"{tp_p:.2f}").classes(
                                                    "text-xs px-1.5 py-0.5 rounded "
                                                    "font-semibold text-white"
                                                ).style("background:#374151")
                                            else:
                                                ui.label("—").classes("text-sm font-mono").style(f"color:{col}")
                                else:
                                    ui.label(val).classes(
                                        "text-sm font-mono font-semibold"
                                    ).style(f"color:{col}")

            rs_chart   = chart_controller.get_risk_settings()
            dpm_active = bool(rs_chart.get("dpm_enabled", 0))
            for t in trades:
                direction = t.get("direction", "?")
                entry_p   = float(t["entry_price"])
                lot_size  = float(t["lot_size"])
                rem_lots  = float(t["remaining_lots"])
                sl        = float(t["stop_loss"]) if t.get("stop_loss") else None
                strategy  = "DPM" if dpm_active else STRATEGY_NAMES.get(t.get("strategy", ""), "")
                ticket    = t.get("mt5_ticket", "—")
                open_time = t.get("open_time", 0)

                current = unreal = 0.0
                if tick:
                    current = tick.bid if direction == "BUY" else tick.ask
                    unreal  = engine.pnl(direction, entry_p, current, rem_lots)

                # Duration
                elapsed = ""
                try:
                    secs = int(datetime.now(timezone.utc).timestamp() - float(open_time))
                    elapsed = (f"{secs // 60}m {secs % 60}s"
                               if secs < 3600 else
                               f"{secs // 3600}h {(secs % 3600) // 60}m")
                except Exception:
                    pass

                triggered = await engine.get_triggered_tps(t["trade_id"])
                dir_col   = _BULL_COL if direction == "BUY" else _BEAR_COL
                p_col     = _pnl_col(unreal)

                with ui.card().classes(
                    "w-full bg-gray-800 rounded-lg p-0 overflow-hidden"
                ):
                    with ui.row().classes(
                        "w-full px-4 py-2 items-center gap-3"
                    ).style("background:#1a2236; border-bottom:1px solid #1f2937"):
                        ui.element("div").classes("w-1 h-5 rounded shrink-0").style(
                            f"background:{dir_col}"
                        )
                        ui.label(f"{direction} XAUUSD").classes(
                            "text-sm font-bold"
                        ).style(f"color:{dir_col}")
                        ui.label(f"#{ticket}").classes("text-xs text-gray-500")
                        ui.label(strategy).classes("text-xs text-gray-500")
                        src_lbl = trade_source_label(t.get("tg_source", ""))
                        ui.badge(src_lbl, color="purple").classes("text-xs")
                        ch = trade_channel_label(t.get("tg_source", ""))
                        if ch and ch != src_lbl:
                            ui.badge(ch, color="indigo").classes("text-xs")
                        ui.space()
                        if t.get("sl_moved_to_be"):
                            ui.label("Breakeven").classes(
                                "text-xs px-2 py-0.5 rounded font-bold text-green-400"
                            ).style("background:rgba(34,197,94,0.12)").tooltip(
                                "Stop Loss has been moved to entry price — "
                                "this trade cannot lose money from the original risk."
                            )
                        ui.label(elapsed).classes("text-xs text-gray-500")

                    closed_lots = round(lot_size - rem_lots, 4)
                    lots_str = (
                        f"{lot_size:.2f} → {rem_lots:.2f}"
                        if closed_lots > 0
                        else f"{lot_size:.2f}"
                    )
                    lots_col = "#fb923c" if closed_lots > 0 else "#e5e7eb"
                    with ui.grid(columns=6).classes("w-full px-4 py-3 gap-x-4 gap-y-1"):
                        for label, val, col in [
                            ("ENTRY",      f"${entry_p:.2f}",                     "#e5e7eb"),
                            ("CURRENT",    f"${current:.2f}" if current else "—", "#e5e7eb"),
                            ("LOTS",       lots_str,                              lots_col),
                            ("SL",         f"${sl:.2f}" if sl else "—",          "#f87171"),
                            ("UNREAL P&L", f"${unreal:+.2f}",                    p_col),
                            ("TPs",        "",                                    "#4ade80"),
                        ]:
                            with ui.column().classes("gap-0"):
                                ui.label(label).classes(
                                    "text-xs text-gray-500 tracking-wider"
                                )
                                if label == "TPs":
                                    with ui.row().classes("gap-1 flex-wrap"):
                                        for n in range(1, 6):
                                            if not t.get(f"tp{n}"):
                                                continue
                                            hit    = n in triggered
                                            bg_col = "#22c55e" if hit else "#374151"
                                            ui.label(f"TP{n}").classes(
                                                "text-xs px-1.5 py-0.5 rounded "
                                                "font-semibold text-white"
                                            ).style(f"background:{bg_col}")
                                else:
                                    ui.label(val).classes(
                                        "text-sm font-mono font-semibold"
                                    ).style(f"color:{col}")

                    with ui.row().classes(
                        "w-full px-4 py-2 gap-2 items-center"
                    ).style("border-top:1px solid #1f2937"):
                        trade_id = t["trade_id"]

                        async def do_close(tid=trade_id):
                            try:
                                await engine.close_trade(tid, "manual_close")
                                ui.notify("Trade closed", type="positive")
                            except Exception as e:
                                ui.notify(str(e), type="negative")

                        ui.button("Close Trade", on_click=do_close).style(
                            "background:#7f1d1d; color:#fff; "
                            "font-size:11px; padding:3px 12px"
                        )

    # ── Candle + indicator refresh (10s) ──────────────────────────────────────

    def _bar_index_for_ts(ts: float) -> int | None:
        """Index of the chart bar containing `ts`, or None if it predates the
        visible window. The FVG overlay has its own timeframe, so its bar
        numbering means nothing on this axis -- only the timestamp does."""
        bar_ts = _state.get("bar_ts") or []
        if not bar_ts or ts < bar_ts[0]:
            return None
        return max(0, bisect_right(bar_ts, ts) - 1)

    def _build_fvg_areas(fvgs: list[dict]) -> list[list[dict]]:
        """ECharts markArea data: each zone is a [start, end] pair.

        Each zone starts at the bar where the gap formed and runs to the
        right edge, which is how a gap actually reads: it is not a level that
        existed before the imbalance that created it. Leaving the x bound off
        entirely (the previous behaviour) drew every zone across the whole
        chart, so a screen of them turned into stacked horizontal bands with
        all the labels piled up on the left.

        Styled to match the reference MT5 chart: a solid block in the gap's
        direction, labelled BUY FVG / SELL FVG. Only two states reach here --
        untested and tested (wicked into but not broken) -- so the tested one
        is drawn a shade lighter rather than given its own colour scheme.
        """
        areas = []
        for f in fvgs:
            bullish = f.get("direction") == "bullish"
            base = "34,197,94" if bullish else "239,68,68"
            # Tested gaps stay drawn but read as the weaker of the two.
            alpha = 0.30 if f.get("filled") else 0.55
            start = {
                "yAxis": f["bottom"],
                "itemStyle": {
                    "color": f"rgba({base},{alpha})",
                    "borderColor": f"rgba({base},0.9)",
                    "borderWidth": 1,
                },
                "label": {
                    "show": True,
                    "formatter": f"{'BUY' if bullish else 'SELL'} FVG",
                    # insideStartTop centres the text on the zone's left edge,
                    # which hangs half the label outside the box; align + a
                    # small offset tucks it inside.
                    "position": "insideStartTop",
                    "align": "left",
                    "verticalAlign": "top",
                    "offset": [5, 3],
                    "color": "#e5e7eb",
                    "fontSize": 10,
                    "fontWeight": "bold",
                },
            }
            bar = _bar_index_for_ts(f["ts"]) if f.get("ts") else None
            if bar is not None:
                # Integer index rather than the axis label: labels repeat
                # across days (bare "13:45" on two dates), and ECharts would
                # resolve the duplicate to the first match.
                start["xAxis"] = bar
            areas.append([start, {"yAxis": f["top"]}])
        return areas

    async def _refresh_fvgs():
        """Recompute the FVG overlay. Deliberately independent of
        _refresh_candles: the overlay has its own timeframe, so it needs its
        own candle fetch rather than reusing whatever the chart is showing."""
        try:
            tf = _state.get("fvg_tf", "off")
            if tf == "off":
                _state["fvgs"] = []
                chart.options["series"][0]["markArea"]["data"] = []
                chart.update()
                return
            from backend.src.services.reversal_engine.ict_patterns import (
                detect_fvgs, select_display_fvgs,
            )
            mt5_tf = TF_MAP.get(tf, "M15")
            # Enough history for gaps to be meaningful without dragging the
            # whole chart's refresh down; FVGs older than this are almost
            # always long since filled.
            candles = await engine.get_candles(mt5_tf, 300)
            if not candles:
                return
            # detect_fvgs is exhaustive on purpose (the ML features want every
            # imbalance); a chart wants only the live ones.
            fvgs = select_display_fvgs(candles, detect_fvgs(candles))
            for f in fvgs:
                # Carry the origin bar's timestamp so the zone can be anchored
                # on a chart running a different timeframe.
                f["ts"] = float(candles[f["idx"]].get("ts") or 0)
            _state["fvgs"] = fvgs
            _state["fvg_last"] = datetime.now(timezone.utc).timestamp()
            chart.options["series"][0]["markArea"]["data"] = _build_fvg_areas(fvgs)
            chart.update()
        except Exception as e:
            log.debug("FVG refresh failed: %s", e)

    async def _refresh_candles():
        try:
            tf     = _state["tf"]
            count  = int(_state["count"])
            mt5_tf = TF_MAP.get(tf, "M15")
            candles = await engine.get_candles(mt5_tf, count)
            if not candles:
                return
            _state["candles"] = candles
            # Bar timestamps, kept so the FVG overlay (which runs on its own
            # timeframe and its own candle fetch) can anchor each zone to the
            # chart bar where the gap actually formed.
            _state["bar_ts"] = [float(c.get("ts") or 0) for c in candles]
            _state["last_candle_refresh"] = datetime.now(timezone.utc).timestamp()

            times:     list[str]   = []
            ohlc_vals: list[list]  = []

            prev_day = None
            for c in candles:
                # Bridge returns Unix timestamp as "ts"; older format used "time"
                raw = c.get("ts") or c.get("time", "")
                try:
                    if isinstance(raw, (int, float)):
                        dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
                    else:
                        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    # Use UTC display — MT5 candle timestamps are UTC+3 stored as UTC,
                    # so treating as UTC gives broker server time matching MT5 display.
                    if tf == "1D":
                        label = dt.strftime("%d/%m")
                    else:
                        day_str = dt.strftime("%d/%m")
                        # Show date prefix whenever the day changes
                        if day_str != prev_day:
                            label    = dt.strftime("%d/%m %H:%M")
                            prev_day = day_str
                        else:
                            label = dt.strftime("%H:%M")
                except Exception:
                    label = str(raw)[:10]
                times.append(label)
                ohlc_vals.append([
                    round(float(c.get("open",  0)), 2),
                    round(float(c.get("close", 0)), 2),
                    round(float(c.get("low",   0)), 2),
                    round(float(c.get("high",  0)), 2),
                ])

            closes = [float(c.get("close", 0)) for c in candles]

            # EMA series (indices 1, 2, 3 in chart.series)
            ema_series = [_ema(closes, p) for p, _ in _EMAS]

            # RSI
            rsi_vals = _rsi14(closes)

            # Update main chart
            chart.options["xAxis"]["data"]     = times
            chart.options["series"][0]["data"] = ohlc_vals
            for i, ev in enumerate(ema_series):
                chart.options["series"][i + 1]["data"] = ev
            chart.update()

            # Update RSI chart
            rsi_chart.options["xAxis"]["data"]     = times
            rsi_chart.options["series"][0]["data"] = rsi_vals
            rsi_chart.update()

            # RSI current value label
            last_rsi = next((v for v in reversed(rsi_vals) if v is not None), None)
            if last_rsi is not None:
                col = ("#FF4444" if last_rsi > 70 else
                       "#00CC88" if last_rsi < 30 else
                       _RSI_COL)
                rsi_lbl.text = f"RSI: {last_rsi:.1f}"
                rsi_lbl.style(f"color:{col}")

        except Exception:
            pass

    # ── Fast refresh (3s): tick, last-candle live update ──────────────────────

    async def _refresh_fast():
        try:
            tick       = await engine.get_tick()
            trades     = await chart_controller.get_open_trades(engine)
            trades     = [t for t in trades if not is_stuck_placeholder(t)]
            untracked  = await engine.get_untracked_mt5_positions()
            _state["tick"] = tick

            candles = _state["candles"]

            if candles and tick:
                existing = chart.options["series"][0]["data"]
                if existing and len(existing) == len(candles):
                    last = list(existing[-1])
                    last[1] = tick.bid            # close = current bid
                    last[2] = min(last[2], tick.bid)  # low
                    last[3] = max(last[3], tick.bid)  # high
                    chart.options["series"][0]["data"][-1] = last

                last_c = candles[-1]
                o  = round(float(last_c.get("open",  0)), 2)
                h  = max(round(float(last_c.get("high", 0)), 2), tick.bid)
                lo = min(round(float(last_c.get("low",  0)), 2), tick.bid)
                c  = tick.bid
                col = "#00CC88" if c >= o else "#FF4444"
                ohlc_lbl.text = f"XAUUSD · {_state['tf']}  O {o}  H {h}  L {lo}  C {c}"
                ohlc_lbl.style(f"color:{col}")

            chart.options["series"][0]["markLine"]["data"] = _build_mark_lines(trades, tick)
            chart.update()

            await _refresh_trades_panel(trades, tick, untracked)

            now = datetime.now().strftime("%H:%M:%S")
            update_lbl.text = f"Live · {now}"

        except Exception:
            pass

    # ── Wire timers ───────────────────────────────────────────────────────────
    ui.timer(10.0, _refresh_candles)
    ui.timer(3.0,  _refresh_fast)

    asyncio.ensure_future(_refresh_candles())
    asyncio.ensure_future(_refresh_fvgs())
    asyncio.ensure_future(_refresh_fast())
