"""History page — equity curve, closed trades table, monthly calendar, AI trade analysis."""

import asyncio
from typing import Callable

from nicegui import ui

import backend.src.config as cfg_module
from frontend.pages import ai_trade_analysis as _ai_analysis

# Imported and unused, deliberately, and only until someone unpicks it:
# runtime.py imports _platform_fee_rate purely to re-export it, and
# tests/refactor/test_runtime_has_no_dead_imports.py treats an external
# `from backend.src.runtime import X` as the thing that justifies it. The
# pre-split history.py was that importer and never called it. Dropping it
# here makes runtime's own import dead and fails that gate, so the chain
# is left exactly as it was -- a split is the wrong commit to unpick it in.
# Tracked in docs/todo/refactor/frontend/restructure/phase2-view-decomposition/031-*.md
# (no trailing noqa: the scanner's regex would swallow it into the name.)
from backend.src.runtime import _platform_fee_rate

from ._calendar import _render_calendar
from ._channels import _render_channels
from ._equity_curve import _render_equity_curve
from ._heatmap import _render_heatmap
from ._shared import (
    _BROKER_OFFSET, _SESSION_LABELS,
    _broker_ts_to_uk_date, _broker_ts_to_utc_hour,
    _entry_deal_comments, _get_market_type_map,
)
from ._trade_table import _render_trade_table

# render() is the page. The four clock/session names are re-exported because
# tests/ui/test_history_session_attribution.py imports them off the package
# root, and they were public there before the split.
__all__ = [
    "render",
    "_BROKER_OFFSET", "_SESSION_LABELS",
    "_broker_ts_to_uk_date", "_broker_ts_to_utc_hour",
    "_entry_deal_comments", "_get_market_type_map",
]

def render(get_engine: Callable):
    engine  = get_engine()
    cfg     = cfg_module.load()
    cur_env = cfg.get("account_env", "demo")
    # ── Performance summary cards (live from MT5) ─────────────────────────────
    def _perf_card(label: str, init: str = "—", val_cls: str = "text-white"):
        """Render one equal-width stat card; returns the value label for updates."""
        with ui.card().classes("bg-gray-800 rounded-lg min-w-0").style("padding:10px 14px;"):
            ui.label(label).classes(
                "text-xs text-gray-400 uppercase tracking-wide leading-tight mb-1"
            )
            val = ui.label(init).classes(
                f"text-base font-bold font-mono leading-none {val_cls}"
            )
        return val

    with ui.column().classes("w-full px-4 pt-3 pb-1 gap-1"):
        with ui.element("div").style(
            "display:grid; grid-template-columns:repeat(8,1fr); gap:12px; width:100%;"
        ):
            closed_lbl = _perf_card("Closed — Daily",       "—",    "text-gray-200")
            wr_lbl     = _perf_card("Win Rate — Daily",     "—%",   "text-green-400")
            pf_lbl     = _perf_card("Profit Factor",        "—",    "text-yellow-300")
            daily_lbl  = _perf_card("Daily P&L",            "$—",   "text-gray-200")
            best_lbl   = _perf_card("Best Trade — Daily",   "$—",   "text-green-400")
            worst_lbl  = _perf_card("Worst Trade — Daily",  "$—",   "text-red-400")
            maxdd_lbl  = _perf_card("Max Drawdown",         "—%",   "text-orange-400")
            roi_lbl    = _perf_card("ROI",                  "—%",   "text-teal-400")
        src_lbl = ui.label("").classes("text-xs text-gray-600")

    async def refresh_perf():
        try:
            p = await engine.compute_mt5_performance(90)
            if p:
                # Daily stats (UK calendar day, midnight → now)
                d_closed = p.get("daily_closed", 0)
                d_wr     = p.get("daily_win_rate_pct", 0.0)
                d_pnl    = p.get("daily_pnl", p.get("daily_pnl_24h", 0.0))
                d_best   = p.get("daily_best", 0.0)
                d_worst  = p.get("daily_worst", 0.0)

                closed_lbl.text = str(d_closed)
                wr_lbl.text     = f"{d_wr:.1f}%"
                pf_lbl.text     = f"{p.get('profit_factor', 0.0):.2f}"
                best_lbl.text   = f"${d_best:+.2f}" if d_closed else "$—"
                worst_lbl.text  = f"${d_worst:+.2f}" if d_closed else "$—"
                maxdd_lbl.text  = f"{p.get('max_drawdown_pct', 0.0):.2f}%"
                roi_pct = p.get("roi_pct", 0.0)
                roi_lbl.text = f"{roi_pct:+.2f}%"
                roi_lbl.classes(replace=(
                    "text-base font-bold font-mono leading-none "
                    + ("text-teal-400" if roi_pct >= 0 else "text-red-400")
                ))
                daily_lbl.text  = f"${d_pnl:+.2f}"
                src_lbl.text    = "MT5 — Local daily"
                # Colour daily P&L by sign
                daily_lbl.classes(replace=(
                    "text-base font-bold font-mono leading-none "
                    + ("text-green-400" if d_pnl >= 0 else "text-red-400")
                ))
                # Colour daily win rate
                wr_lbl.classes(replace=(
                    "text-base font-bold font-mono leading-none "
                    + ("text-green-400" if d_wr >= 50 else "text-red-400")
                ))
        except Exception:
            pass

    ui.timer(15.0, refresh_perf)
    asyncio.ensure_future(refresh_perf())

    # ── Equity Curve — always visible, full-width, above the sub-tabs ────────────
    _render_equity_curve(engine)

    # ── Sub-tabs: Trade History | Calendar | Heat Map | Channels | AI | DPM ──────
    with ui.tabs().classes("bg-gray-800") as htabs:
        t_trades   = ui.tab("Trade History",      icon="table_rows")
        t_calendar = ui.tab("Calendar",           icon="calendar_month")
        t_heatmap  = ui.tab("Heat Map",           icon="grid_on")
        t_channels = ui.tab("Channels",           icon="leaderboard")
        t_ai       = ui.tab("AI TRADE ANALYSIS",  icon="smart_toy")
        t_dpm      = ui.tab("DPM Analysis",       icon="auto_graph")

    with ui.tab_panels(htabs, value=t_trades).classes("bg-gray-900").style("padding:0"):

        with ui.tab_panel(t_trades).style("padding:16px"):
            _render_trade_table(engine)

        with ui.tab_panel(t_calendar).style("padding:16px"):
            _render_calendar(engine)

        with ui.tab_panel(t_heatmap).style("padding:16px"):
            _render_heatmap(engine)

        with ui.tab_panel(t_channels).style("padding:16px"):
            _render_channels(engine)

        with ui.tab_panel(t_ai).style("padding:16px"):
            _ai_analysis.render(get_engine)

        with ui.tab_panel(t_dpm).style("padding:16px"):
            from frontend.pages import dpm_analysis as _dpm_analysis
            _dpm_analysis.render()


# ── Equity curve ───────────────────────────────────────────────────────────────

# ── Trade history table (live from MT5) ───────────────────────────────────────

# Short display names for the strategy column






# The reference GoldSnipers copier EA stamps its own per-channel comment on
# every position it opens: "C<slot>_<sigcode>_<ANC|PEN>" (see
# core_ea_templates.py and ForexTraderBridge.mq5, both of which quote observed
# live examples like "C2_LDBD_25202_ANC"). Those positions are NOT this app's
# trades -- they have no vantage_simulated_trades row and never will -- so no
# channel can honestly be attributed to them. They were previously
# indistinguishable from a genuine attribution failure: blank ("—") in the
# Closed Trades table and "Unknown" in the calendar day detail. Naming the
# copier says what actually happened instead. The slot number is the copier's
# own per-channel input block (InpC{n}_*), not a Telegram channel name this
# app knows, so it is shown as-is rather than guessed at.




# ── Market type classifier (from signal generator analysis logs) ───────────────

# ── Monthly calendar ───────────────────────────────────────────────────────────

# ── Hour × Weekday P&L heat map ─────────────────────────────────────────────────

# ── Channel scorecard + adaptive sizing ─────────────────────────────────────────

