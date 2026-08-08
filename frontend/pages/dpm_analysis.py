"""DPM Analysis page — per-trade outcomes, what DPM learned, calibration history."""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from nicegui import ui

import backend.src.config as cfg_module
from backend.src.controllers import dpm_controller as dpm_controller

# ── Helpers ───────────────────────────────────────────────────────────────────

def _uk(ts) -> str:
    """Format an MT5 broker timestamp for display.
    MT5 timestamps are UTC+3 encoded as Unix epoch; treating as UTC gives broker time."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _usd(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"${sign}{v:.2f}"


def _r(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}R"


def _regime_color(regime: Optional[str]) -> str:
    return {
        "trending": "text-green-400",
        "ranging":  "text-amber-400",
        "spike":    "text-red-400",
    }.get((regime or "").lower(), "text-gray-400")


def _exit_color(exit_type: Optional[str]) -> str:
    t = (exit_type or "").upper()
    if t == "SL":
        return "text-red-400"
    if t in ("TP", "ALL_TPS_HIT", "PROFIT_CLOSE_TARGET"):
        return "text-green-400"
    if "BE" in t or "BREAKEVEN" in t:
        return "text-amber-400"
    return "text-gray-300"


def _milestones(row: dict) -> str:
    parts = []
    if row.get("reached_be"):
        parts.append("BE")
    if row.get("reached_tp1"):
        parts.append("TP1")
    if row.get("reached_tp2"):
        parts.append("TP2")
    return " > ".join(parts) if parts else "none"


# The three DB reads this page used to run itself now live behind
# backend.src.controllers.dpm_controller (M3 page drain): same queries, same
# off-loop dispatch, plain dicts back.


def _summary_stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r.get("closed_at")]
    total  = len(rows)
    done   = len(closed)
    if not closed:
        return {"total": total, "done": done, "win_rate": 0.0, "avg_r": 0.0,
                "profit_factor": 0.0, "avg_hold": 0.0, "calibrated_pct": 0.0,
                "be_triggered": 0, "tp1_reached": 0}

    pnls    = [float(r.get("final_pnl") or 0) for r in closed]
    r_mults = [float(r.get("r_multiple") or 0) for r in closed]
    wins    = [p for p in pnls if p > 0]
    losses  = [abs(p) for p in pnls if p < 0]

    win_rate      = len(wins) / len(pnls) * 100 if pnls else 0.0
    avg_r         = sum(r_mults) / len(r_mults) if r_mults else 0.0
    profit_factor = sum(wins) / sum(losses) if losses else (float("inf") if wins else 0.0)
    avg_hold      = sum(float(r.get("hold_minutes") or 0) for r in closed) / len(closed)
    cal_count     = sum(1 for r in closed if r.get("used_calibrated"))
    cal_pct       = cal_count / len(closed) * 100 if closed else 0.0
    be_trig       = sum(1 for r in closed if r.get("reached_be"))
    tp1_hit       = sum(1 for r in closed if r.get("reached_tp1"))

    return {
        "total": total, "done": done, "win_rate": win_rate, "avg_r": avg_r,
        "profit_factor": profit_factor, "avg_hold": avg_hold,
        "calibrated_pct": cal_pct, "be_triggered": be_trig, "tp1_reached": tp1_hit,
    }


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    """Build the DPM Analysis tab content."""

    # ── Summary cards ─────────────────────────────────────────────────────────
    # Use CSS grid so all 8 cards are exactly equal width (1fr each).
    # flex-1 / flex-wrap cannot guarantee this because a card's intrinsic
    # minimum width (driven by its longest word) inflates it past 1/N.
    with ui.element("div").style(
        "display:grid; grid-template-columns:repeat(8,1fr); gap:12px; width:100%; margin-bottom:8px;"
    ):
        def _stat_card(label: str, value_ref: list, color: str = "text-white"):
            with ui.card().classes("bg-gray-800 rounded-lg min-w-0").style("padding:12px 16px;"):
                ui.label(label).classes(
                    "text-xs text-gray-400 uppercase tracking-wide leading-tight"
                )
                lbl = ui.label("—").classes(f"text-xl font-bold {color} mt-1")
                value_ref.append(lbl)

        wr_ref    = []
        avgr_ref  = []
        pf_ref    = []
        hold_ref  = []
        total_ref = []
        cal_ref   = []
        be_ref    = []
        tp1_ref   = []

        _stat_card("Win Rate",      wr_ref,    "text-green-400")
        _stat_card("Avg R-Multiple", avgr_ref, "text-blue-300")
        _stat_card("Profit Factor",  pf_ref,   "text-yellow-300")
        _stat_card("Avg Hold",       hold_ref, "text-gray-200")
        _stat_card("Trades (total)", total_ref,"text-gray-200")
        _stat_card("Calibrated %",   cal_ref,  "text-purple-300")
        _stat_card("BE Triggered",   be_ref,   "text-amber-400")
        _stat_card("TP1 Reached",    tp1_ref,  "text-green-300")

    # ── Learning progress banner ───────────────────────────────────────────────
    progress_label = ui.label("").classes("text-xs text-gray-400 mb-2")

    # ── Trade Performance table ────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 rounded-lg overflow-hidden mb-4"):
        with ui.row().classes("items-center justify-between px-4 py-2"):
            ui.label("Per-Trade DPM Outcomes").classes("font-semibold text-yellow-300")
            ui.label("What DPM did on each trade").classes("text-xs text-gray-400")

        trade_table = ui.table(
            columns=[
                {"name": "opened",    "label": "Opened",     "field": "opened",    "align": "left"},
                {"name": "ticket",    "label": "Ticket",     "field": "ticket",    "align": "right"},
                {"name": "channel",   "label": "Channel",    "field": "channel",   "align": "left"},
                {"name": "dir",       "label": "Dir",        "field": "dir",       "align": "center"},
                {"name": "regime",    "label": "Regime",     "field": "regime",    "align": "center"},
                {"name": "session",   "label": "Session",    "field": "session",   "align": "center"},
                {"name": "momentum",  "label": "Momentum",   "field": "momentum",  "align": "center"},
                {"name": "be_mult",   "label": "BE Mult",    "field": "be_mult",   "align": "center"},
                {"name": "trail",     "label": "Trail Dist", "field": "trail",     "align": "center"},
                {"name": "milestones","label": "Milestones", "field": "milestones","align": "center"},
                {"name": "peak",      "label": "Peak P&L",   "field": "peak",      "align": "right"},
                {"name": "final_pnl", "label": "Final P&L",  "field": "final_pnl", "align": "right"},
                {"name": "r_mult",    "label": "R-Mult",     "field": "r_mult",    "align": "right"},
                {"name": "exit",      "label": "Exit",       "field": "exit",      "align": "center"},
                {"name": "hold",      "label": "Hold",       "field": "hold",      "align": "center"},
                {"name": "cal",       "label": "Calibrated", "field": "cal",       "align": "center"},
            ],
            rows=[],
            row_key="trade_id_raw",
        ).classes("w-full").props("dense flat dark")

        trade_table.add_slot("body-cell-dir", """
            <q-td :props="props">
                <q-badge :color="props.value === 'BUY' ? 'green' : 'red'" :label="props.value"/>
            </q-td>
        """)
        trade_table.add_slot("body-cell-regime", """
            <q-td :props="props">
                <span :class="{
                    'text-green-400': props.value === 'trending',
                    'text-amber-400': props.value === 'ranging',
                    'text-red-400':   props.value === 'spike',
                    'text-gray-400':  !props.value || props.value === '—',
                }">{{ props.value || '—' }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-final_pnl", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-r_mult", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-exit", """
            <q-td :props="props">
                <span :class="{
                    'text-red-400':   props.value === 'SL',
                    'text-green-400': ['TP','all_tps_hit','profit_close_target'].includes(props.value),
                    'text-amber-400': props.value && props.value.includes('BE'),
                }">{{ props.value || '—' }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-cal", """
            <q-td :props="props">
                <q-badge :color="props.value === 'Yes' ? 'purple' : 'grey'" :label="props.value"/>
            </q-td>
        """)

    # ── Calibration — what DPM has learned ────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 rounded-lg overflow-hidden mb-4"):
        with ui.row().classes("items-center justify-between px-4 py-2"):
            ui.label("DPM Learned Parameters (latest calibration)").classes("font-semibold text-yellow-300")
            cal_date_label = ui.label("").classes("text-xs text-gray-400")

        cal_table = ui.table(
            columns=[
                {"name": "session",    "label": "Session",     "field": "session",    "align": "left"},
                {"name": "momentum",   "label": "Momentum",    "field": "momentum",   "align": "center"},
                {"name": "be_mult",    "label": "BE Mult",     "field": "be_mult",    "align": "center"},
                {"name": "trail_mult", "label": "Trail Mult",  "field": "trail_mult", "align": "center"},
                {"name": "tp1_pct",    "label": "TP1 Close %", "field": "tp1_pct",    "align": "center"},
                {"name": "samples",    "label": "Samples",     "field": "samples",    "align": "center"},
                {"name": "win_rate",   "label": "Win Rate",    "field": "win_rate",   "align": "center"},
                {"name": "avg_r",      "label": "Avg R",       "field": "avg_r",      "align": "center"},
                {"name": "pf",         "label": "Prof. Factor","field": "pf",         "align": "center"},
                {"name": "notes",      "label": "Notes",       "field": "notes",      "align": "left"},
            ],
            rows=[],
            row_key="session_momentum",
        ).classes("w-full").props("dense flat dark")

    # ── Calibration run history ────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 rounded-lg overflow-hidden mb-4"):
        with ui.row().classes("items-center px-4 py-2"):
            ui.label("Calibration Run History").classes("font-semibold text-yellow-300")

        run_table = ui.table(
            columns=[
                {"name": "run_date",  "label": "Run Date",     "field": "run_date",  "align": "left"},
                {"name": "samples",   "label": "Samples",      "field": "samples",   "align": "center"},
                {"name": "buckets",   "label": "Buckets",      "field": "buckets",   "align": "center"},
                {"name": "avg_wr",    "label": "Avg Win Rate",  "field": "avg_wr",   "align": "center"},
                {"name": "avg_r",     "label": "Avg R",         "field": "avg_r",    "align": "center"},
                {"name": "avg_pf",    "label": "Avg Prof. F",   "field": "avg_pf",   "align": "center"},
            ],
            rows=[],
            row_key="run_date",
        ).classes("w-full").props("dense flat dark")

    # ── Regime breakdown chart ─────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 rounded-lg p-4 mb-4"):
        with ui.row().classes("items-center mb-2"):
            ui.label("Regime Performance Breakdown").classes("font-semibold text-yellow-300")
        regime_chart = ui.echart({
            "backgroundColor": "transparent",
            "tooltip": {"trigger": "axis"},
            "legend": {"textStyle": {"color": "#d1d5db"}, "top": 0},
            "xAxis": {
                "type": "category", "data": [],
                "axisLabel": {"color": "#e5e7eb"},
            },
            "yAxis": [
                {"type": "value", "name": "Win %",
                 "axisLabel": {"color": "#d1d5db"},
                 "splitLine": {"lineStyle": {"color": "#1f2937", "type": "dashed"}}},
                {"type": "value", "name": "Avg R",
                 "axisLabel": {"color": "#d1d5db"},
                 "splitLine": {"show": False}},
            ],
            "series": [
                {"name": "Win %",   "type": "bar",  "data": [], "yAxisIndex": 0,
                 "itemStyle": {"color": "#22d3ee"}},
                {"name": "Avg R",   "type": "line", "data": [], "yAxisIndex": 1,
                 "lineStyle": {"color": "#a78bfa"}, "itemStyle": {"color": "#a78bfa"}},
            ],
        }).classes("w-full h-48")

    # ── Refresh logic ─────────────────────────────────────────────────────────

    async def _refresh():
        # The controller runs each read on the DB worker thread; running them
        # directly on the event loop blocked the whole app every 30s.
        perf_rows = await dpm_controller.get_perf_rows()
        stats     = _summary_stats(perf_rows)
        cal_rows  = await dpm_controller.get_calibration_rows()
        run_rows  = await dpm_controller.get_calibration_runs()

        # Update summary cards
        wr_ref[0].text    = _pct(stats["win_rate"])
        avgr_ref[0].text  = _r(stats["avg_r"])
        pf_v = stats["profit_factor"]
        pf_ref[0].text    = f"{pf_v:.2f}" if pf_v != float("inf") else "inf"
        hold_v = stats["avg_hold"]
        hold_ref[0].text  = (f"{int(hold_v)}m" if hold_v < 60
                              else f"{int(hold_v // 60)}h {int(hold_v % 60)}m")
        total_ref[0].text = f"{stats['done']} / {stats['total']}"
        cal_ref[0].text   = _pct(stats["calibrated_pct"])
        be_ref[0].text    = str(stats["be_triggered"])
        tp1_ref[0].text   = str(stats["tp1_reached"])

        # Learning progress
        done_count = stats["done"]
        if done_count < 20:
            remaining = 20 - done_count
            progress_label.text = (
                f"DPM needs {remaining} more closed trade(s) before first self-calibration "
                f"(currently {done_count}/20). Calibration triggers every 5 new trades (2h min gap)."
            )
        else:
            progress_label.text = (
                f"Self-calibration active — {done_count} closed DPM trades on record. "
                "Recalibrates after every 5 new trades (2h minimum gap between runs)."
            )

        # Trade table rows
        table_rows = []
        for r in perf_rows:
            hold_m = float(r.get("hold_minutes") or 0)
            if hold_m < 60:
                hold_str = f"{int(hold_m)}m"
            else:
                hold_str = f"{int(hold_m // 60)}h {int(hold_m % 60)}m"
            table_rows.append({
                "trade_id_raw":  r.get("trade_id", ""),
                "opened":        _uk(r.get("opened_at")),
                "ticket":        str(r.get("mt5_ticket") or "—"),
                "channel":       r.get("tg_source") or "—",
                "dir":           r.get("direction", "—"),
                "regime":        r.get("regime_at_entry") or "—",
                "session":       (r.get("session_at_entry") or "—").upper(),
                "momentum":      r.get("momentum_label") or "—",
                "be_mult":       f'{float(r.get("be_multiplier_used") or 0):.2f}x',
                "trail":         f'${float(r.get("trail_dist_used") or 0):.1f}',
                "milestones":    _milestones(r),
                "peak":          f'${float(r.get("peak_pnl") or 0):+.2f}',
                "final_pnl":     f'{float(r.get("final_pnl") or 0):+.2f}',
                "r_mult":        f'{float(r.get("r_multiple") or 0):+.2f}',
                "exit":          r.get("exit_type") or ("open" if not r.get("closed_at") else "—"),
                "hold":          hold_str if r.get("closed_at") else "running",
                "cal":           "Yes" if r.get("used_calibrated") else "No",
            })
        trade_table.rows = table_rows
        trade_table.update()

        # Calibration table
        if cal_rows:
            cal_ts = cal_rows[0].get("calibrated_at")
            cal_date_label.text = f"Run: {_uk(cal_ts)}" if cal_ts else ""
            cal_table.rows = [
                {
                    "session_momentum": f'{r.get("session","")}-{r.get("momentum_bucket","")}',
                    "session":    (r.get("session") or "—").upper(),
                    "momentum":   r.get("momentum_bucket") or "—",
                    "be_mult":    f'{float(r.get("be_multiplier") or 0):.2f}x',
                    "trail_mult": f'{float(r.get("trail_multiplier") or 0):.2f}x',
                    "tp1_pct":    _pct((r.get("tp1_partial_pct") or 0) * 100),
                    "samples":    str(r.get("sample_size", 0)),
                    "win_rate":   _pct(r.get("win_rate")),
                    "avg_r":      _r(r.get("avg_r_multiple")),
                    "pf":         f'{float(r.get("profit_factor") or 0):.2f}',
                    "notes":      r.get("notes") or "",
                }
                for r in cal_rows
            ]
        else:
            cal_date_label.text = "No calibration run yet"
            cal_table.rows = []
        cal_table.update()

        # Run history table
        run_table.rows = [
            {
                "run_date": _uk(r.get("calibrated_at")),
                "samples":  str(int(r.get("total_samples") or 0)),
                "buckets":  str(int(r.get("buckets") or 0)),
                "avg_wr":   _pct(r.get("avg_win_rate")),
                "avg_r":    _r(r.get("avg_r")),
                "avg_pf":   f'{float(r.get("avg_pf") or 0):.2f}',
            }
            for r in run_rows
        ]
        run_table.update()

        # Regime chart
        regime_map: dict[str, list] = {}
        for r in perf_rows:
            if not r.get("closed_at"):
                continue
            regime = (r.get("regime_at_entry") or "unknown").lower()
            if regime not in regime_map:
                regime_map[regime] = []
            regime_map[regime].append({
                "pnl": float(r.get("final_pnl") or 0),
                "r":   float(r.get("r_multiple") or 0),
            })

        reg_labels, win_pcts, avg_rs = [], [], []
        for reg in ("trending", "ranging", "spike", "unknown"):
            data = regime_map.get(reg)
            if not data:
                continue
            wins_r = sum(1 for d in data if d["pnl"] > 0)
            reg_labels.append(reg.capitalize())
            win_pcts.append(round(wins_r / len(data) * 100, 1))
            avg_rs.append(round(sum(d["r"] for d in data) / len(data), 2))

        regime_chart.options["xAxis"]["data"] = reg_labels
        regime_chart.options["series"][0]["data"] = win_pcts
        regime_chart.options["series"][1]["data"] = avg_rs
        regime_chart.update()

    # Initial load + periodic refresh
    asyncio.create_task(_refresh())
    ui.timer(30.0, _refresh)
