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
from typing import Callable

from nicegui import ui

from backend.src.controllers import chart_controller as chart_controller
from backend.src.services.positions.core_indicators import ema_series, rsi_series
from backend.src.controllers.trading_controller import is_stuck_placeholder

from ._overlays import _build_fvg_areas, _build_mark_lines
from ._trades_panel import _refresh_trades_panel

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

    # ── Trades panel renderer ─────────────────────────────────────────────────

    # ── Candle + indicator refresh (10s) ──────────────────────────────────────

    def _bar_index_for_ts(ts: float) -> int | None:
        """Index of the chart bar containing `ts`, or None if it predates the
        visible window. The FVG overlay has its own timeframe, so its bar
        numbering means nothing on this axis -- only the timestamp does."""
        bar_ts = _state.get("bar_ts") or []
        if not bar_ts or ts < bar_ts[0]:
            return None
        return max(0, bisect_right(bar_ts, ts) - 1)

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
            chart.options["series"][0]["markArea"]["data"] = _build_fvg_areas(fvgs, _bar_index_for_ts=_bar_index_for_ts)
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

            await _refresh_trades_panel(
                trades, tick, untracked, engine=engine, trades_panel=trades_panel,
            )

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
