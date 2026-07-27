"""
Edge Dashboard — per-engine performance analytics computed live from the
engine databases. Surfaces the numbers that decide whether an engine has an
edge: profit factor, expectancy, win rate, avg win vs avg loss, and a
day-of-week x hour P&L heatmap that exposes toxic time windows (the breakout
engine's 12:00-15:00 UTC losses hid in aggregate stats for weeks).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from nicegui import ui

log = logging.getLogger(__name__)

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Engine registry: label → (db filename, table, pnl expression, time column)
# pnl expression must be a closed-trade net-dollar value.
_ENGINES: dict[str, dict] = {
    "Breakout": {
        "db": "breakout_signal.db", "table": "bo_signals",
        "pnl": "net_pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
    "Bounce": {
        "db": "test_signal.db", "table": "test_signals",
        "pnl": "pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
    "Reversal Engine": {
        "db": "reversal_engine.db", "table": "re_signals",
        "pnl": "net_pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
}


def _data_dir() -> Path:
    from backend.src.config import DATA_DIR
    return Path(DATA_DIR)


def _query(db_file: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Read-only query against an engine DB; missing DB returns []."""
    path = _data_dir() / db_file
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
            return conn.execute(sql, params).fetchall()
    except Exception as e:
        log.debug("[Edge] query failed on %s: %s", db_file, e)
        return []


def _engine_stats(cfg: dict, days: int) -> dict:
    cutoff = time.time() - days * 86400 if days > 0 else 0
    rows = _query(cfg["db"], f"""
        SELECT {cfg['pnl']} FROM {cfg['table']}
        WHERE outcome IN ('win','loss','be') AND {cfg['pnl']} IS NOT NULL
          AND COALESCE(close_time, 0) >= ?
    """, (cutoff,))
    pnls = [float(r[0]) for r in rows]
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p < 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    return {
        "n":          n,
        "net":        sum(pnls),
        "pf":         (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "win_rate":   len(wins) / n,
        "expectancy": sum(pnls) / n,
        "avg_win":    (gross_w / len(wins)) if wins else 0.0,
        "avg_loss":   (gross_l / len(losses)) if losses else 0.0,
    }


def _heatmap_data(cfg: dict, days: int) -> list[list]:
    """Return [[hour, weekday_idx, net_pnl], ...] for closed trades."""
    cutoff = time.time() - days * 86400 if days > 0 else 0
    rows = _query(cfg["db"], f"""
        SELECT CAST(strftime('%H', {cfg['tcol']}, 'unixepoch') AS INT)  AS h,
               CAST(strftime('%w', {cfg['tcol']}, 'unixepoch') AS INT)  AS dow,
               ROUND(SUM({cfg['pnl']}), 2)
        FROM {cfg['table']}
        WHERE outcome IN ('win','loss','be') AND {cfg['pnl']} IS NOT NULL
          AND COALESCE(close_time, 0) >= ?
        GROUP BY h, dow
    """, (cutoff,))
    # SQLite %w: 0=Sunday … 6=Saturday → remap to Mon=0 … Sun=6
    return [[int(h), (int(dow) + 6) % 7, float(v or 0)] for h, dow, v in rows]


def _hour_totals(cfg: dict, days: int) -> dict[int, float]:
    out: dict[int, float] = {}
    for h, _dow, v in _heatmap_data(cfg, days):
        out[h] = out.get(h, 0.0) + v
    return out


def render(get_engine=None) -> None:  # get_engine unused; matches page signature
    state = {"engine": "Breakout", "days": 0}

    with ui.column().classes("w-full gap-4 p-4"):
        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("Edge Dashboard").classes("text-xl font-bold text-yellow-400")
            ui.space()
            days_sel = ui.select(
                {0: "All time", 7: "Last 7 days", 30: "Last 30 days"},
                value=0, label="Window",
            ).classes("w-36").props("dense dark outlined")
            refresh_btn = ui.button("Refresh", icon="refresh").props("flat dense")

        cards_row = ui.row().classes("w-full gap-4 flex-wrap")

        with ui.row().classes("items-center gap-4 w-full mt-2"):
            ui.label("P&L by hour and weekday (UTC)").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider")
            engine_sel = ui.select(
                list(_ENGINES.keys()), value="Breakout",
            ).classes("w-40").props("dense dark outlined")

        chart_box = ui.column().classes("w-full")
        insight_box = ui.column().classes("w-full gap-1")

        ui.label("Telegram → order pipeline latency").classes(
            "text-sm font-semibold text-gray-400 uppercase tracking-wider mt-2")
        latency_box = ui.row().classes("w-full gap-4 flex-wrap")

    def _build_cards() -> None:
        cards_row.clear()
        with cards_row:
            for name, cfg in _ENGINES.items():
                s = _engine_stats(cfg, state["days"])
                with ui.card().classes("rounded-lg p-4 min-w-56 bg-gray-800"):
                    ui.label(name).classes("text-base font-bold text-yellow-400")
                    if s["n"] == 0:
                        ui.label("No closed trades").classes("text-xs text-gray-500")
                        continue
                    net = s["net"]
                    pf  = s["pf"]
                    net_color = "text-green-400" if net >= 0 else "text-red-400"
                    pf_color  = "text-green-400" if pf >= 1.0 else "text-red-400"
                    ui.label(f"${net:+,.2f}").classes(f"text-2xl font-bold {net_color}")
                    with ui.grid(columns=2).classes("gap-x-6 gap-y-1 text-xs"):
                        ui.label("Profit factor").classes("text-gray-400")
                        ui.label("∞" if pf == float("inf") else f"{pf:.2f}").classes(pf_color)
                        ui.label("Win rate").classes("text-gray-400")
                        ui.label(f"{s['win_rate']:.0%} ({s['n']} trades)")
                        ui.label("Expectancy / trade").classes("text-gray-400")
                        ui.label(f"${s['expectancy']:+.2f}")
                        ui.label("Avg win / avg loss").classes("text-gray-400")
                        ratio = (s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else float("inf")
                        ui.label(
                            f"${s['avg_win']:.0f} / ${s['avg_loss']:.0f} "
                            f"({'∞' if ratio == float('inf') else f'{ratio:.2f}'})"
                        )

    def _build_heatmap() -> None:
        chart_box.clear()
        insight_box.clear()
        cfg  = _ENGINES[state["engine"]]
        data = _heatmap_data(cfg, state["days"])
        with chart_box:
            if not data:
                ui.label("No closed trades for this engine/window").classes("text-gray-500")
                return
            lim = max(abs(v) for _, _, v in data) or 1.0
            ui.echart({
                "tooltip": {"position": "top"},
                "grid": {"height": "70%", "top": "5%", "left": 60, "right": 20},
                "xAxis": {
                    "type": "category", "data": [f"{h:02d}" for h in range(24)],
                    "splitArea": {"show": True},
                    "axisLabel": {"color": "#9ca3af"},
                },
                "yAxis": {
                    "type": "category", "data": _WEEKDAYS,
                    "splitArea": {"show": True},
                    "axisLabel": {"color": "#9ca3af"},
                },
                "visualMap": {
                    "min": -lim, "max": lim, "calculable": True,
                    "orient": "horizontal", "left": "center", "bottom": 0,
                    "inRange": {"color": ["#dc2626", "#1f2937", "#16a34a"]},
                    "textStyle": {"color": "#9ca3af"},
                },
                "series": [{
                    "type": "heatmap",
                    "data": data,      # [hour, weekday, value]
                    "label": {"show": False},
                    "emphasis": {"itemStyle": {"shadowBlur": 8}},
                }],
            }).classes("w-full h-80")

        # Best / worst hours — the actionable summary
        totals = _hour_totals(cfg, state["days"])
        if totals:
            ranked = sorted(totals.items(), key=lambda kv: kv[1])
            worst  = [f"{h:02d}:00 (${v:+,.0f})" for h, v in ranked[:3] if v < 0]
            best   = [f"{h:02d}:00 (${v:+,.0f})" for h, v in ranked[-3:][::-1] if v > 0]
            with insight_box:
                if worst:
                    ui.label("Worst hours: " + ",  ".join(worst)).classes("text-xs text-red-400")
                if best:
                    ui.label("Best hours:  " + ",  ".join(best)).classes("text-xs text-green-400")

    _STAGE_LABELS = {
        "queue_wait_ms":    "Msg → dequeued (asyncio scheduling)",
        "buffer_ms":        "Dequeued → buffered",
        "scanner_wake_ms":  "Buffered → scanner processing",
        "decision_ms":      "Scanning → decision",
        "order_ms":         "Decision → MT5 order sent",
        "total_ms":         "Total (arrival → order sent)",
    }

    def _build_latency() -> None:
        latency_box.clear()
        with latency_box:
            try:
                from forex_trader.core import latency_trace as _lt
                from forex_trader.core import loop_monitor as _lm
            except Exception:
                ui.label("Latency tracing unavailable").classes("text-xs text-gray-500")
                return

            stage_summary = _lt.summary()
            with ui.card().classes("rounded-lg p-4 min-w-72 bg-gray-800"):
                ui.label("Pipeline stages (ms)").classes("text-sm font-bold text-yellow-400")
                any_data = False
                with ui.grid(columns=4).classes("gap-x-4 gap-y-1 text-xs mt-1"):
                    ui.label("stage").classes("text-gray-500")
                    ui.label("p50").classes("text-gray-500")
                    ui.label("p90").classes("text-gray-500")
                    ui.label("max").classes("text-gray-500")
                    for key, label in _STAGE_LABELS.items():
                        s = stage_summary.get(key) or {}
                        if not s:
                            continue
                        any_data = True
                        ui.label(label).classes("text-gray-300")
                        ui.label(f"{s['p50']:.0f}")
                        ui.label(f"{s['p90']:.0f}")
                        ui.label(f"{s['max']:.0f}")
                if not any_data:
                    ui.label("No traced messages yet this session").classes(
                        "text-xs text-gray-500 mt-1")

            loop_summary = _lm.summary()
            with ui.card().classes("rounded-lg p-4 min-w-56 bg-gray-800"):
                ui.label("Event-loop stalls").classes("text-sm font-bold text-yellow-400")
                if not loop_summary.get("n"):
                    ui.label("No stalls detected — loop is responsive").classes(
                        "text-xs text-green-400 mt-1")
                else:
                    ui.label(f"{loop_summary['n']} stalls this session").classes(
                        "text-xs text-red-400")
                    ui.label(
                        f"p50 {loop_summary['p50']:.0f}ms · p90 {loop_summary['p90']:.0f}ms "
                        f"· max {loop_summary['max']:.0f}ms"
                    ).classes("text-xs text-gray-400")
                    latest = loop_summary.get("latest") or {}
                    if latest.get("tasks"):
                        ui.label("Last stall — running: " + ", ".join(latest["tasks"][:4])).classes(
                            "text-xs text-gray-500")

    def _rebuild() -> None:
        state["days"]   = int(days_sel.value or 0)
        state["engine"] = engine_sel.value or "Breakout"
        _build_cards()
        _build_heatmap()
        _build_latency()

    refresh_btn.on_click(_rebuild)
    days_sel.on_value_change(_rebuild)
    engine_sel.on_value_change(_rebuild)

    _rebuild()
