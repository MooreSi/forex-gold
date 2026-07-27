"""History page — equity curve, closed trades table, monthly calendar, AI trade analysis."""

import asyncio
import calendar
import json as _json
import re as _re
import sqlite3
import time
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo as _ZoneInfo
from pathlib import Path
from typing import Callable, Optional

from nicegui import ui

import backend.src.config as cfg_module
from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.engine import _apply_fee, _platform_fee_rate
from forex_trader.core.models import STRATEGY_NAMES, CONTRACT_SIZE
from forex_trader.ui.pages import ai_trade_analysis as _ai_analysis
from forex_trader.ui.pages.trading import trade_source_label, trade_channel_label


def _parse_reason(comment: str, pnl: float = 0.0) -> str:
    """
    Translate a raw MT5 close-deal comment into a clean human-readable label.

    MT5 close comment patterns (from Vantage / standard MT5):
      "[sl 4482.00]"       → SL
      "[tp 4460.00]"       → TP
      "so: 1:100"          → Stop-out (margin call)
      "closePosition"      → Manual close (via bridge)
      "close"              → Manual close
      "ForexTrader"        → App-initiated close
      ""                   → fallback on PnL sign
    """
    c = comment.strip().lower()
    if c.startswith("[sl") or "stop loss" in c or c == "sl":
        return "SL"
    if c.startswith("[tp") or "take profit" in c or c == "tp":
        return "TP"
    if c.startswith("so:") or "stop out" in c or "margin call" in c:
        return "Stop-out"
    if c in ("closeposition", "close", "forextrader", "manual", ""):
        return "Manual"
    # Partial-close comments from the app
    if "partial" in c or "scale" in c:
        return "Partial TP"
    # Anything left — return cleaned-up original
    return comment.strip() or ("Win" if pnl > 0 else "Loss")


def _get_env_db_path(env: str) -> str:
    """Return the absolute DB path for the given environment."""
    from backend.src.config import DATA_DIR
    return str(DATA_DIR / f"forex_trader_{env}.db")


def _query_env_db(env: str, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a SELECT against the given environment's database."""
    db_path = _get_env_db_path(env)
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _uk(ts) -> str:
    """Format an MT5 broker timestamp for display.
    MT5 timestamps are UTC+3 encoded as Unix epoch; treating as UTC gives broker time."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _fmt_duration(seconds: Optional[float]) -> str:
    """Compact human-readable duration -- "45s", "12m", "2h 15m", "3d 4h".
    Used for both how long a closed trade was held (open->close) and how
    long a Limit Runner/EA Template grid order sat pending before it filled
    (pending_placed_at->open)."""
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _to_date(ts) -> Optional[date]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except Exception:
        return None


_UK_TZ = _ZoneInfo("Europe/London")
_BROKER_OFFSET = 10800  # broker stores UTC+3 timestamps as-if-UTC


def _broker_ts_to_uk_date(ts) -> Optional[date]:
    """Convert a broker timestamp (UTC+3-stored-as-UTC) to UK local calendar date."""
    try:
        real_utc_epoch = float(ts) - _BROKER_OFFSET
        return datetime.fromtimestamp(real_utc_epoch, tz=_UK_TZ).date()
    except Exception:
        return None


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
            maxru_lbl  = _perf_card("Max Run-Up",           "—%",   "text-teal-400")
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
                maxru_lbl.text  = f"{p.get('max_runup_pct', 0.0):.2f}%"
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
            from forex_trader.ui.pages import dpm_analysis as _dpm_analysis
            _dpm_analysis.render()


# ── Equity curve ───────────────────────────────────────────────────────────────

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
                env = cfg_module.get("account_env", "demo")
                starting = float(cfg_module.get("starting_balance", 1000.0))
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
                except Exception:
                    pass

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
                    x_data.append(_uk(ts))
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
            except Exception:
                pass

        ui.timer(15.0, refresh_chart)
        asyncio.ensure_future(refresh_chart())


# ── Trade history table (live from MT5) ───────────────────────────────────────

# Short display names for the strategy column
_STRAT_LABEL = {
    "scale_out":       "Scale Out",
    "be_runner":       "BE Runner",
    "trail_stop":      "Trail Stop",
    "protected_scale": "Protected Scale",
    "conservative":    "Conservative",
}


def _strategy_display_label(strategy: str) -> str:
    """Human-readable label for a trade's strategy, including EA Templates
    ("template:<name>") -- these are user-defined, not one of the fixed
    built-in strategies, so they were never in STRATEGY_NAMES/_STRAT_LABEL
    and fell through to the "—" placeholder instead of a readable name.
    Confirmed live 2026-07-23 that every EA Template trade showed a blank
    Strategy column in Trade Analysis."""
    if not strategy:
        return "—"
    from forex_trader.core import core_ea_templates as _et
    if _et.is_template_override(strategy):
        return f"Template: {_et.template_name_from_override(strategy)}"
    return _STRAT_LABEL.get(strategy, STRATEGY_NAMES.get(strategy, "—"))


def _render_trade_table(engine):
    def _ticket_source_map(days: int) -> dict[str, str]:
        """Return {mt5_ticket_str: channel_or_source_label}.

        Built primarily from THIS node's own vantage_simulated_trades — but
        History's "Closed Trades" view reads MT5's real broker-side deal
        history, which is the same shared account both a Mac and a paired
        VPS trade against. A ticket opened by the OTHER node has no local
        row here at all, so it fell through to a blank channel. The
        consolidated ledger (populated by both nodes over the sync channel)
        fills exactly that gap — local data always wins where both exist.

        `days` bounds the local queries to the same window the page is
        showing -- this table only grows over a long-running session, and
        there is no reason to build a map covering every trade ever opened
        when the caller only needs the tickets in the selected period.
        """
        cutoff = time.time() - days * 86400
        result: dict[str, str] = {}
        try:
            ledger_channels, _, _ = db_module.get_consolidated_ticket_maps()
            for ticket, tg_source in ledger_channels.items():
                ch = trade_channel_label(tg_source or "")
                result[ticket] = ch if ch else trade_source_label(tg_source or "")
        except Exception:
            pass
        try:
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT mt5_ticket, tg_source FROM vantage_simulated_trades "
                    "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
                    (cutoff,),
                ).fetchall()
            for mt5_ticket, tg_source in rows:
                ch = trade_channel_label(tg_source or "")
                result[str(mt5_ticket)] = ch if ch else trade_source_label(tg_source or "")
        except Exception:
            pass
        try:
            # Adaptive Runner ladder legs 2+ (see core/engine.py's
            # _open_adaptive_runner_ladder) are opened as their own raw MT5
            # tickets, tracked only in vantage_ladder_legs — they never get
            # their own vantage_simulated_trades row, so the query above
            # never finds them and they fell back to whatever stale/generic
            # label the consolidated ledger happened to have (or none at
            # all). Inherit the real channel from the parent trade instead.
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT l.mt5_ticket, t.tg_source FROM vantage_ladder_legs l "
                    "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
                    "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
                    (cutoff,),
                ).fetchall()
            for mt5_ticket, tg_source in rows:
                ch = trade_channel_label(tg_source or "")
                result[str(mt5_ticket)] = ch if ch else trade_source_label(tg_source or "")
        except Exception:
            pass
        return result

    def _ticket_strategy_map(days: int) -> dict[str, str]:
        """Return {mt5_ticket_str: strategy_display}.
        Shows 'DPM' when the trade has a dpm_trade_performance record (DPM managed it),
        otherwise falls back to the base strategy stored on the trade row.
        Same cross-node fallback and `days` windowing as _ticket_source_map — see its docstring.
        """
        cutoff = time.time() - days * 86400
        result: dict[str, str] = {}
        try:
            _, ledger_strategies, _ = db_module.get_consolidated_ticket_maps()
            for ticket, strategy in ledger_strategies.items():
                result[ticket] = _strategy_display_label(strategy or "")
        except Exception:
            pass
        try:
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT t.mt5_ticket, t.strategy, d.trade_id "
                    "FROM vantage_simulated_trades t "
                    "LEFT JOIN dpm_trade_performance d ON t.trade_id = d.trade_id "
                    "WHERE t.mt5_ticket IS NOT NULL AND t.open_time >= ?",
                    (cutoff,),
                ).fetchall()
            for mt5_ticket, strategy, dpm_trade_id in rows:
                if dpm_trade_id:
                    label = "DPM"
                else:
                    label = _strategy_display_label(strategy or "")
                result[str(mt5_ticket)] = label
        except Exception:
            pass
        try:
            # Same ladder-leg gap as _ticket_source_map — legs 2+ have no
            # vantage_simulated_trades row of their own, only a
            # vantage_ladder_legs row; inherit the parent's strategy.
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT l.mt5_ticket, t.strategy FROM vantage_ladder_legs l "
                    "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
                    "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
                    (cutoff,),
                ).fetchall()
            for mt5_ticket, strategy in rows:
                result[str(mt5_ticket)] = _strategy_display_label(strategy or "")
        except Exception:
            pass
        return result

    def _ticket_max_tp_map() -> dict[str, str]:
        """Return {mt5_ticket_str: max_tp_hit_display} from vantage_simulated_trades.
        Falls back to the consolidated ledger for tickets the OTHER node opened —
        same cross-node gap/fallback pattern as _ticket_source_map."""
        result: dict[str, str] = {}
        try:
            ledger_max_tp, _ = db_module.get_consolidated_extra_maps()
            result.update(ledger_max_tp)
        except Exception:
            pass
        try:
            result.update(db_module.get_max_tp_map_by_ticket())
        except Exception:
            pass
        return result

    def _ticket_rr_map() -> dict[str, float]:
        """Return {mt5_ticket_str: rr} — R:R computed from the trade's own final
        entry/stop/tp1 levels. Falls back to the consolidated ledger for tickets
        the OTHER node opened — same cross-node fallback pattern as _ticket_source_map."""
        result: dict[str, float] = {}
        try:
            _, ledger_rr = db_module.get_consolidated_extra_maps()
            result.update(ledger_rr)
        except Exception:
            pass
        try:
            result.update(db_module.get_rr_map_by_ticket())
        except Exception:
            pass
        return result

    def _ticket_order_type_map(days: int) -> dict[str, tuple[str, Optional[float]]]:
        """Return {mt5_ticket_str: (order_type, pending_placed_at)} from this
        node's own vantage_simulated_trades. order_type/pending_placed_at
        (2026-07-23) aren't part of the Local/Remote consolidated-ledger sync
        protocol yet, so — unlike every other map above — a ticket the OTHER
        node opened just falls back to "Market"/no pending time here rather
        than showing the real value; local-only for now."""
        cutoff = time.time() - days * 86400
        result: dict[str, tuple[str, Optional[float]]] = {}
        try:
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT mt5_ticket, order_type, pending_placed_at FROM vantage_simulated_trades "
                    "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
                    (cutoff,),
                ).fetchall()
            for mt5_ticket, order_type, pending_placed_at in rows:
                result[str(mt5_ticket)] = (order_type or "market", pending_placed_at)
        except Exception:
            pass
        return result

    def _ticket_group_map() -> dict[str, tuple[str, int]]:
        """Return {mt5_ticket_str: (trade_id, tier)} for every ticket that is
        part of an Adaptive Runner ladder (one signal split into N resting
        broker-side TP tickets — see core/engine.py's
        _open_adaptive_runner_ladder). Used to collapse all of a signal's
        legs into a single row in the Closed Trades table instead of N flat
        rows, since from the user's perspective TP1-TP8 are tiers of ONE
        trade, not N separate trades — the multi-ticket split is purely an
        MT5-hedging-mode implementation detail (a ticket's native TP applies
        to its whole volume, so genuine broker-side partial profit-taking at
        each tier requires N tickets; see the ladder work's own docstring).
        Trades with only 1 recorded leg (e.g. the "no post-fill TP left"
        fallback) are intentionally excluded by the caller — grouping a
        group of one is pointless.
        """
        result: dict[str, tuple[str, int]] = {}
        try:
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT t.trade_id, t.mt5_ticket AS ticket, 1 AS tier "
                    "FROM vantage_simulated_trades t "
                    "WHERE t.trade_id IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs) "
                    "AND t.mt5_ticket IS NOT NULL "
                    "UNION ALL "
                    "SELECT l.trade_id, l.mt5_ticket AS ticket, l.tier "
                    "FROM vantage_ladder_legs l WHERE l.mt5_ticket IS NOT NULL"
                ).fetchall()
            for trade_id, ticket, tier in rows:
                result[str(ticket)] = (trade_id, int(tier))
        except Exception:
            pass
        return result

    with ui.card().classes("w-full bg-gray-800 rounded-lg overflow-hidden"):
        with ui.row().classes("items-center justify-between px-4 py-2 flex-wrap gap-2"):
            ui.label("Closed Trades").classes("font-semibold text-yellow-300")
            status_lbl = ui.label("").classes("text-xs text-orange-400 italic")
            days_sel = ui.select(
                {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days",
                 60: "Last 60 days", 90: "Last 90 days", 180: "Last 6 months"},
                value=7, label="Period",
            ).classes("w-36")
            src_badge = ui.badge("MT5", color="green").classes("text-xs")
            refresh_btn = ui.button("Refresh", icon="refresh").classes(
                "bg-gray-700 text-white text-xs px-3 py-1"
            )

        # Create the table ONCE — update rows in-place rather than destroying/recreating.
        _columns = [
            {"name": "time",      "label": "Closed",      "field": "time",      "align": "left",   "sortable": True},
            {"name": "ticket",    "label": "Ticket",      "field": "ticket",    "align": "right"},
            {"name": "channel",   "label": "Channel",     "field": "channel",   "align": "left"},
            {"name": "order_type","label": "Order Type",  "field": "order_type","align": "center"},
            {"name": "direction", "label": "Dir",         "field": "direction", "align": "center", "sortable": True},
            {"name": "entry",     "label": "Entry",       "field": "entry",     "align": "right"},
            {"name": "exit",      "label": "Exit",        "field": "exit",      "align": "right"},
            {"name": "pips",      "label": "Pips",        "field": "pips",      "align": "right",  "sortable": True},
            {"name": "lots",      "label": "Lots",        "field": "lots",      "align": "right"},
            {"name": "strategy",  "label": "Strategy",    "field": "strategy",  "align": "center"},
            {"name": "spread",    "label": "Spread",      "field": "spread",    "align": "right",  "sortable": True},
            {"name": "fees",      "label": "Fees",        "field": "fees",      "align": "right",  "sortable": True},
            {"name": "pnl",       "label": "Net P&L",     "field": "pnl",       "align": "right",  "sortable": True},
            {"name": "reason",    "label": "Reason",      "field": "reason",    "align": "center"},
            {"name": "rr",        "label": "R:R",         "field": "rr",        "align": "right",  "sortable": True},
            {"name": "max_tp",    "label": "Max TP",      "field": "max_tp",    "align": "center", "sortable": True},
            {"name": "duration",  "label": "Held",        "field": "duration",  "align": "right",  "sortable": True},
            {"name": "pending",   "label": "Pending For", "field": "pending",   "align": "right",  "sortable": True},
        ]
        trade_table = ui.table(
            columns=_columns, rows=[], row_key="_id"
        ).classes("w-full").props("dense flat dark")
        # Adaptive Runner ladder legs collapse into one row per signal (see
        # _ticket_group_map) — clicking that row expands/collapses its legs.
        _expanded_groups: set[str] = set()

        def _on_row_click(e):
            row = e.args[1] if isinstance(e.args, list) and len(e.args) > 1 else (e.args or {})
            gid = row.get("_group_id") if isinstance(row, dict) else None
            if not gid:
                return
            if gid in _expanded_groups:
                _expanded_groups.discard(gid)
            else:
                _expanded_groups.add(gid)
            asyncio.create_task(refresh_table())

        trade_table.on("rowClick", _on_row_click)

        trade_table.add_slot("body-cell-ticket", """
            <q-td :props="props">
                <span v-if="props.row._is_group"
                      style="font-weight:600;color:#fbbf24;cursor:pointer;">
                    {{ props.value }}
                </span>
                <span v-else-if="props.row._is_leg" style="color:#9ca3af;font-size:12px;">
                    {{ props.value }}
                </span>
                <span v-else>{{ props.value }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-direction", """
            <q-td :props="props">
                <q-badge :color="props.value === 'BUY' ? 'green' : 'red'" :label="props.value"/>
            </q-td>
        """)
        trade_table.add_slot("body-cell-fees", """
            <q-td :props="props">
                <span :class="{'text-gray-400': props.value === '$0.00', 'text-orange-400': props.value !== '$0.00'}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-pnl", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-pips", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0, 'text-gray-400': props.value === '—'}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-reason", """
            <q-td :props="props">
                <span :class="{
                    'text-red-400':   props.value === 'SL',
                    'text-green-400': props.value === 'TP' || props.value === 'Partial TP',
                    'text-amber-400': props.value === 'Manual',
                    'text-orange-400': props.value === 'Stop-out',
                }">{{ props.value || '—' }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-rr", """
            <q-td :props="props">
                <span v-if="props.value === '—'" style="color:#6b7280;">—</span>
                <span v-else style="color:#93c5fd;">{{ props.value }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-max_tp", """
            <q-td :props="props">
                <span v-if="props.value === '...'"
                      style="color:#6b7280;font-size:11px;" title="Updating in 30 min">...</span>
                <span v-else-if="props.value === 'none'"
                      style="color:#6b7280;">—</span>
                <q-badge v-else-if="props.value"
                         :label="props.value"
                         :color="props.value === 'TP1' ? 'teal' : props.value === 'TP2' ? 'green' : 'purple'"
                         style="font-size:11px;font-weight:600;"/>
                <span v-else style="color:#374151;font-size:10px;" title="30-min window not yet elapsed">–</span>
            </q-td>
        """)

        async def refresh_table():
            """Fetch closed trades exclusively from MT5 deal history."""
            mt5_error: Optional[str] = None
            rows_by_ticket: dict[int, dict] = {}
            # Offloaded — these are all synchronous DB reads; running them
            # directly on the event loop blocked the whole app every 15s.
            _days_now  = int(days_sel.value)
            src_map    = await db_module.to_db_thread(_ticket_source_map, _days_now)
            strat_map  = await db_module.to_db_thread(_ticket_strategy_map, _days_now)
            max_tp_map = await db_module.to_db_thread(_ticket_max_tp_map)
            rr_map     = await db_module.to_db_thread(_ticket_rr_map)
            order_type_map = await db_module.to_db_thread(_ticket_order_type_map, _days_now)
            comm_rate  = await db_module.to_db_thread(_platform_fee_rate)
            try:
                deals = await engine._bridge.get_deal_history(int(days_sel.value))
                if deals:
                    by_pos: dict[int, list] = {}
                    for d in deals:
                        pid = d.get("position_id")
                        if pid:  # excludes None and 0 (balance/deposit ops)
                            by_pos.setdefault(int(pid), []).append(d)

                    # Spread paid at entry is a historical fact — it never changes once
                    # computed, so cache it permanently and only ask MT5 for tickets
                    # this table has never seen before (this loop re-runs every 15s).
                    spread_cache = await db_module.to_db_thread(
                        db_module.get_cached_spreads, list(by_pos.keys())
                    )

                    # Fetch every still-uncached ticket's entry-time tick concurrently
                    # instead of one sequential await per ticket inside the row loop
                    # below — with a wide date range (or a cold cache) that loop was
                    # awaiting hundreds of individual MT5 bridge round-trips one at a
                    # time, each blocking the next.
                    _needs_spread: list[tuple[int, float, float]] = []  # (ticket, open_ts, open_lots)
                    for ticket, pos_deals in by_pos.items():
                        if spread_cache.get(ticket) is not None:
                            continue
                        _open = next((d for d in pos_deals if d.get("entry") == 0), None)
                        if not _open:
                            continue
                        _open_ts = float(_open.get("time", 0))
                        if _open_ts:
                            _needs_spread.append((ticket, _open_ts, float(_open.get("volume", 0))))

                    if _needs_spread:
                        _ticks = await asyncio.gather(
                            *(engine._bridge.get_tick_at(ts) for _, ts, _ in _needs_spread),
                            return_exceptions=True,
                        )
                        for (ticket, _ts, open_lots), tick_at in zip(_needs_spread, _ticks):
                            if not tick_at or isinstance(tick_at, BaseException):
                                continue
                            sp_price = round(float(tick_at["ask"]) - float(tick_at["bid"]), 5)
                            sp_points = round(sp_price / 0.01, 1)
                            sp_cost = round(sp_price * open_lots * CONTRACT_SIZE, 2)
                            spread_cache[ticket] = {
                                "spread_price": sp_price, "spread_points": sp_points,
                                "spread_cost_usd": sp_cost,
                            }
                            await db_module.to_db_thread(
                                db_module.cache_spread, ticket, sp_price, sp_points, sp_cost
                            )

                    for ticket, pos_deals in by_pos.items():
                        open_deal  = next((d for d in pos_deals if d.get("entry") == 0), None)
                        _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
                        close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
                        if not close_deal:
                            continue

                        if open_deal:
                            direction = "BUY" if int(open_deal.get("type", 0)) == 0 else "SELL"
                            entry     = float(open_deal.get("price", 0))
                            open_lots = float(open_deal.get("volume", 0))
                        else:
                            close_type = int(close_deal.get("type", 0))
                            direction  = "SELL" if close_type == 0 else "BUY"
                            entry      = 0.0
                            open_lots  = float(close_deal.get("volume", 0))

                        exit_p       = float(close_deal.get("price", 0))
                        pnl, fees_display = _apply_fee(pos_deals, open_lots, comm_rate)
                        close_ts = float(close_deal.get("time", 0))
                        open_ts  = float(open_deal.get("time", 0)) if open_deal else 0.0
                        reason   = _parse_reason(close_deal.get("comment") or "", pnl)

                        # Order Type / Pending For: distinguishes a genuine
                        # resting Limit Runner/EA Template grid fill from an
                        # immediate market open, and shows how long it sat on
                        # the broker's book before it filled -- so a widening
                        # gap between placement and fill (stale pending
                        # orders) can be spotted and weighed against its P&L.
                        _otype, _pending_at = order_type_map.get(str(ticket), ("market", None))
                        order_type_display = "Limit" if _otype == "limit" else "Market"
                        pending_display = (
                            _fmt_duration(open_ts - _pending_at)
                            if _pending_at and open_ts else "—"
                        )
                        duration_display = _fmt_duration(close_ts - open_ts) if open_ts and close_ts else "—"

                        # Spread paid at entry — already embedded in pnl via MT5's real
                        # fill prices; shown here as an informational cost breakdown, not
                        # a further deduction (Vantage Standard STP: 0% commission, spread-only).
                        spread_display = "—"
                        cached_spread = spread_cache.get(ticket)
                        if cached_spread:
                            spread_display = f"{cached_spread['spread_points']:.1f}pt"
                            fees_display = cached_spread["spread_cost_usd"]

                        # Pips: Vantage XAUUSD — 1 pip = $0.10 price change
                        if entry and exit_p:
                            raw_pips = (exit_p - entry) if direction == "BUY" else (entry - exit_p)
                            pips_val = raw_pips * 10
                            pips_str = f"{pips_val:+.1f}"
                        else:
                            pips_val = 0.0
                            pips_str = "—"

                        # Show partial close breakdown in the lots column
                        close_vols = [float(d.get("volume", 0)) for d in _cd]
                        if len(_cd) > 1:
                            fills_str = " + ".join(f"{v:.2f}" for v in close_vols)
                            lots_display = f"{open_lots:.2f} (P/C: {fills_str})"
                        else:
                            lots_display = f"{open_lots:.2f}"

                        # Max TP: blank = 30-min window not yet elapsed,
                        # "..." = window elapsed but computation pending,
                        # "none"/TP label = computed result.
                        _now = time.time()
                        _max_tp_raw = max_tp_map.get(str(ticket))
                        if _max_tp_raw is not None:
                            max_tp_display = _max_tp_raw   # "none" or "TP1".."TP8"
                        elif close_ts and (_now - close_ts) >= 1800:
                            max_tp_display = "..."          # window elapsed, engine pending
                        else:
                            max_tp_display = ""             # 30-min window not yet up

                        _rr_raw = rr_map.get(str(ticket))
                        rr_display = f"{_rr_raw:.2f}:1" if _rr_raw is not None else "—"

                        rows_by_ticket[ticket] = {
                            "_ts":       close_ts,
                            "_pnl":      pnl,
                            "_pips":     pips_val,
                            "_lots":     open_lots,
                            "_fees":     fees_display,
                            "_entry":    entry,
                            "_id":       str(ticket),
                            "time":      _uk(close_ts),
                            "ticket":    str(ticket),
                            "channel":   src_map.get(str(ticket), "—"),
                            "order_type": order_type_display,
                            "direction": direction,
                            "entry":     f"{entry:.2f}" if entry else "—",
                            "exit":      f"{exit_p:.2f}",
                            "pips":      pips_str,
                            "strategy":  strat_map.get(str(ticket), "—"),
                            "lots":      lots_display,
                            "spread":    spread_display,
                            "fees":      f"${fees_display:.2f}",
                            "pnl":       f"{pnl:+.2f}",
                            "reason":    reason,
                            "rr":        rr_display,
                            "max_tp":    max_tp_display,
                            "duration":  duration_display,
                            "pending":   pending_display,
                        }
            except Exception as exc:
                mt5_error = str(exc)

            group_map = await db_module.to_db_thread(_ticket_group_map)
            groups: dict[str, list[dict]] = {}
            flat_rows: list[dict] = []
            for ticket, row in rows_by_ticket.items():
                gid, tier = group_map.get(str(ticket), (None, None))
                if gid:
                    row["_tier"] = tier
                    groups.setdefault(gid, []).append(row)
                else:
                    flat_rows.append(row)

            for gid, members in groups.items():
                if len(members) == 1:
                    flat_rows.append(members[0])
                    continue
                members.sort(key=lambda r: r["_tier"])
                anchor = next((m for m in members if m["_tier"] == 1), members[0])
                total_lots  = sum(m["_lots"] for m in members)
                total_pnl   = sum(m["_pnl"] for m in members)
                total_fees  = sum(m["_fees"] for m in members)
                tp_hits     = [m for m in members if m["reason"] == "TP"]
                last_close  = max(members, key=lambda m: m["_ts"])
                weighted_pips = (
                    sum(m["_pips"] * m["_lots"] for m in members) / total_lots
                    if total_lots else 0.0
                )
                expanded = gid in _expanded_groups
                arrow = "▾" if expanded else "▸"
                flat_rows.append({
                    "_ts":         last_close["_ts"],
                    "_id":         f"group:{gid}",
                    "_is_group":   True,
                    "_group_id":   gid,
                    "_leg_count":  len(members),
                    "time":        last_close["time"],
                    "ticket":      f"{arrow} {len(members)} legs",
                    "channel":     anchor["channel"],
                    "order_type":  anchor["order_type"],
                    "direction":   anchor["direction"],
                    "entry":       anchor["entry"],
                    "exit":        "Multiple",
                    "pips":        f"{weighted_pips:+.1f}",
                    "strategy":    anchor["strategy"],
                    "lots":        f"{total_lots:.2f}",
                    "spread":      "—",
                    "fees":        f"${total_fees:.2f}",
                    "pnl":         f"{total_pnl:+.2f}",
                    "reason":      f"{len(tp_hits)}/{len(members)} TP",
                    "rr":          anchor["rr"],
                    "max_tp":      anchor["max_tp"],
                    "duration":    anchor["duration"],
                    "pending":     anchor["pending"],
                })
                if expanded:
                    for m in members:
                        m["_id"] = f"leg:{m['ticket']}"
                        m["_is_leg"] = True
                        m["_group_id"] = gid
                        m["ticket"] = f"↳ {m['ticket']} (T{m['_tier']})"
                        flat_rows.append(m)

            rows = sorted(flat_rows, key=lambda r: r["_ts"], reverse=True)

            src_badge.props("color=green" if rows else "color=grey")
            src_badge.text = f"MT5 ({len(rows)})" if rows else "MT5"
            status_lbl.text = f"MT5 error: {mt5_error}" if mt5_error else ""

            trade_table.rows = rows
            trade_table.update()

        async def _on_refresh_click():
            await refresh_table()

        days_sel.on("update:model-value", lambda _: asyncio.create_task(refresh_table()))
        refresh_btn.on("click", _on_refresh_click)
        ui.timer(15.0, refresh_table)
        asyncio.ensure_future(refresh_table())


# ── Market type classifier (from signal generator analysis logs) ───────────────

def _get_market_type_map(year: int, month: int) -> dict:
    """
    Returns {date: (label, colour)} for each day in the month using ADX values
    stored in test_analysis_log.adx (written by the signal generator every scan cycle).

    ADX thresholds (averaged across all scans on that day):
      ≥ 50  →  Strong Trend  (red)    — bad for bounce system
      ≥ 35  →  Trending      (orange) — risky for bounce
      ≥ 20  →  Mixed         (slate)  — transitional / moderate
      < 20  →  Ranging       (teal)   — ideal for bounce system

    A directional arrow (↑/↓) is appended when HTF bias is consistently
    bullish or bearish across the day's scans.
    """
    result: dict[date, tuple[str, str]] = {}
    try:
        from backend.src.config import DATA_DIR
        sig_db = DATA_DIR / "test_signal.db"
        if not sig_db.exists():
            return result

        if month == 12:
            next_month_first = date(year + 1, 1, 1)
        else:
            next_month_first = date(year, month + 1, 1)

        ts_start = datetime.combine(date(year, month, 1), datetime.min.time()).timestamp()
        ts_end   = datetime.combine(next_month_first,     datetime.min.time()).timestamp()

        conn = sqlite3.connect(str(sig_db), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, adx, htf_bias FROM test_analysis_log "
            "WHERE ts >= ? AND ts < ? AND adx IS NOT NULL",
            (ts_start, ts_end),
        ).fetchall()
        conn.close()

        # Group ADX + bias samples by date
        day_adx:  dict[date, list[float]] = {}
        day_bias: dict[date, list[str]]   = {}
        for r in rows:
            d = _to_date(r["ts"])
            if not d:
                continue
            day_adx.setdefault(d, []).append(float(r["adx"]))
            if r["htf_bias"]:
                day_bias.setdefault(d, []).append(r["htf_bias"])

        for d, adx_list in day_adx.items():
            avg    = sum(adx_list) / len(adx_list)
            biases = day_bias.get(d, [])
            # Dominant HTF bias directional arrow
            if biases:
                bull = biases.count("bullish")
                bear = biases.count("bearish")
                if bull > bear * 1.5:
                    arrow = " ↑"
                elif bear > bull * 1.5:
                    arrow = " ↓"
                else:
                    arrow = ""
            else:
                arrow = ""

            if avg >= 50:
                result[d] = (f"Strong Trend{arrow}", "#ef4444")
            elif avg >= 35:
                result[d] = (f"Trending{arrow}",     "#f97316")
            elif avg >= 20:
                result[d] = (f"Mixed{arrow}",        "#94a3b8")
            else:
                result[d] = (f"Ranging{arrow}",      "#2dd4bf")

    except Exception:
        pass

    return result


# ── Monthly calendar ───────────────────────────────────────────────────────────

def _render_calendar(engine):
    _state = {"year": datetime.now(_UK_TZ).year, "month": datetime.now(_UK_TZ).month}

    header_row  = ui.row().classes("w-full items-center gap-3 mb-3")
    stats_lbl   = ui.label("Loading...").classes("text-sm text-gray-400")
    source_lbl  = ui.label("").classes("text-xs text-gray-500 ml-2")
    content     = ui.column().classes("w-full")

    _detail_store: dict = {}   # {date: [ {ticket, source, strategy, direction, pnl}, ... ]}

    def _ticket_info() -> dict:
        """{ticket_str: (source_label, strategy_label, direction)}.

        Same cross-node fallback as _ticket_source_map/_ticket_strategy_map
        above — this node's own vantage_simulated_trades has no row at all
        for a ticket the OTHER node opened, so without merging in the
        consolidated ledger every VPS-origin (or Mac-origin, from the VPS's
        point of view) ticket showed "Unknown" in the calendar day-detail
        view even though the Closed Trades table already had this fixed.
        """
        info: dict[str, tuple] = {}
        try:
            ledger_channels, ledger_strategies, ledger_directions = db_module.get_consolidated_ticket_maps()
            for ticket, tg_source in ledger_channels.items():
                ch = trade_channel_label(tg_source or "")
                label = ch if ch else trade_source_label(tg_source or "")
                strat = ledger_strategies.get(ticket, "")
                info[ticket] = (
                    label,
                    _strategy_display_label(strat or ""),
                    ledger_directions.get(ticket, ""),
                )
        except Exception:
            pass
        try:
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT mt5_ticket, tg_source, strategy, direction "
                    "FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
                ).fetchall()
            for tk, src, strat, dir_ in rows:
                ch = trade_channel_label(src or "")
                label = ch if ch else trade_source_label(src or "")
                # Local data always wins where both exist.
                info[str(tk)] = (label, _strategy_display_label(strat or ""), dir_ or "")
        except Exception:
            pass
        try:
            # Same ladder-leg gap as _ticket_source_map/_ticket_strategy_map
            # — legs 2+ have no vantage_simulated_trades row of their own.
            with db_module.db() as conn:
                rows = conn.execute(
                    "SELECT l.mt5_ticket, t.tg_source, t.strategy, t.direction "
                    "FROM vantage_ladder_legs l "
                    "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
                    "WHERE l.mt5_ticket IS NOT NULL"
                ).fetchall()
            for tk, src, strat, dir_ in rows:
                ch = trade_channel_label(src or "")
                label = ch if ch else trade_source_label(src or "")
                info[str(tk)] = (label, _strategy_display_label(strat or ""), dir_ or "")
        except Exception:
            pass
        return info

    async def _build_day_map(year: int, month: int) -> tuple[dict, dict, str]:
        """Build day-level P&L map exclusively from MT5 deal history."""
        trade_map: dict[int, tuple[date, float]] = {}  # ticket → (date, pnl)
        _detail_store.clear()
        # Offloaded — both are synchronous DB reads.
        _tinfo    = await db_module.to_db_thread(_ticket_info)
        comm_rate = await db_module.to_db_thread(_platform_fee_rate)

        try:
            today_d   = datetime.now(_UK_TZ).date()
            first     = date(year, month, 1)
            days_back = max((today_d - first).days + 35, 35)
            deals     = await engine._bridge.get_deal_history(int(days_back))

            if deals:
                by_pos: dict[int, list] = {}
                for d in deals:
                    pid = d.get("position_id")
                    if pid:  # excludes None and 0 (balance/deposit ops)
                        by_pos.setdefault(int(pid), []).append(d)

                for ticket, pos_deals in by_pos.items():
                    _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
                    close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
                    if not close_deal:
                        continue
                    close_ts = close_deal.get("time")
                    if not close_ts:
                        continue
                    d_date = _broker_ts_to_uk_date(close_ts)
                    if not d_date or d_date.year != year or d_date.month != month:
                        continue
                    # Net P&L including estimated fees (_apply_fee), matching the
                    # Closed Trades table and equity curve — not raw MT5 profit,
                    # which is always fee-free on a demo account and would
                    # understate real trading cost everywhere else in this file.
                    open_deal = next((d for d in pos_deals if d.get("entry") == 0), None)
                    open_lots = float(open_deal.get("volume", 0)) if open_deal else float(close_deal.get("volume", 0))
                    pnl, _fees = _apply_fee(pos_deals, open_lots, comm_rate)
                    trade_map[ticket] = (d_date, pnl)
        except Exception:
            pass

        day_map: dict[date, dict] = {}
        for _ticket, (d, pnl) in trade_map.items():
            if d not in day_map:
                day_map[d] = {"pnl": 0.0, "trades": 0, "wins": 0}
            day_map[d]["pnl"]    += pnl
            day_map[d]["trades"] += 1
            if pnl > 0:
                day_map[d]["wins"] += 1
            src, strat, dir_ = _tinfo.get(str(_ticket), ("Unknown", "—", ""))
            _detail_store.setdefault(d, []).append({
                "ticket": _ticket, "source": src, "strategy": strat,
                "direction": dir_, "pnl": pnl,
            })

        source = f"MT5 ({len(trade_map)} trades)" if trade_map else "no data"
        market_map = _get_market_type_map(year, month)
        return day_map, market_map, source

    _sun_cal = calendar.Calendar(firstweekday=6)  # Sunday-first

    def _pnl_fmt(v: float) -> str:
        if abs(v) < 10000:
            return f"${v:+,.2f}"
        return f"${v/1000:+.1f}K"

    # ── Persistent day-detail dialog ──────────────────────────────────────────
    # Created once here (outside content/grid) so that content.clear() on
    # auto-refresh never destroys it while it is open.
    _day_dialog = ui.dialog().props("persistent")
    with _day_dialog:
        with ui.card().classes("bg-gray-900 p-4 rounded-lg").style("min-width:560px"):
            _dialog_body = ui.column().classes("w-full gap-1")
            ui.button("Close", on_click=_day_dialog.close).classes(
                "bg-gray-700 text-white mt-3"
            )

    def _show_day_detail(d: date):
        """Populate and open the persistent day-detail dialog."""
        trades = _detail_store.get(d, [])
        _dialog_body.clear()
        with _dialog_body:
            day_total = sum(t["pnl"] for t in trades)
            wins      = sum(1 for t in trades if t["pnl"] > 0)
            wr        = wins / len(trades) * 100 if trades else 0.0
            with ui.row().classes("items-center justify-between w-full mb-2"):
                ui.label(d.strftime("%A, %d %B %Y")).classes("text-base font-bold text-yellow-300")
                ui.label(_pnl_fmt(day_total)).classes(
                    "text-base font-bold font-mono " +
                    ("text-green-400" if day_total >= 0 else "text-red-400")
                )
            if not trades:
                ui.label("No trades on this day.").classes("text-gray-500 italic")
                _day_dialog.open()
                return
            ui.label(f"{len(trades)} trades · {wr:.0f}% win rate").classes("text-xs text-gray-400 mb-2")

            # ── Per-source breakdown ──────────────────────────────────────────
            by_src: dict = {}
            for t in trades:
                s = by_src.setdefault(t["source"], {"n": 0, "w": 0, "pnl": 0.0})
                s["n"] += 1
                s["pnl"] += t["pnl"]
                if t["pnl"] > 0:
                    s["w"] += 1
            ui.label("By Signal Source").classes("text-xs font-semibold text-gray-300 uppercase tracking-wider mt-1")
            src_rows = [
                {"source": k,
                 "trades": v["n"],
                 "win_rate": f"{(v['w']/v['n']*100 if v['n'] else 0):.0f}%",
                 "pnl": f"${v['pnl']:+.2f}"}
                for k, v in sorted(by_src.items(), key=lambda x: -x[1]["pnl"])
            ]
            ui.table(
                columns=[
                    {"name": "source", "label": "Source", "field": "source", "align": "left"},
                    {"name": "trades", "label": "Trades", "field": "trades", "align": "right"},
                    {"name": "win_rate", "label": "Win%", "field": "win_rate", "align": "right"},
                    {"name": "pnl", "label": "Net P&L", "field": "pnl", "align": "right"},
                ],
                rows=src_rows, row_key="source",
            ).classes("w-full mb-3").props("dense flat dark")

            # ── Individual trades ─────────────────────────────────────────────
            ui.label("Trades").classes("text-xs font-semibold text-gray-300 uppercase tracking-wider")
            trade_rows = [
                {"ticket": str(t["ticket"]), "source": t["source"],
                 "direction": t["direction"], "strategy": t["strategy"],
                 "pnl": f"${t['pnl']:+.2f}"}
                for t in sorted(trades, key=lambda x: -x["pnl"])
            ]
            ui.table(
                columns=[
                    {"name": "ticket", "label": "Ticket", "field": "ticket", "align": "left"},
                    {"name": "source", "label": "Source", "field": "source", "align": "left"},
                    {"name": "direction", "label": "Dir", "field": "direction", "align": "center"},
                    {"name": "strategy", "label": "Strategy", "field": "strategy", "align": "left"},
                    {"name": "pnl", "label": "P&L", "field": "pnl", "align": "right"},
                ],
                rows=trade_rows, row_key="ticket",
            ).classes("w-full").props("dense flat dark")
        _day_dialog.open()

    def _draw_grid(day_map: dict, market_map: dict, year: int, month: int):
        content.clear()
        with content:
            # Day-of-week headers — full width grid
            with ui.grid(columns=7).classes("w-full gap-1.5 mb-1"):
                for day_name in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]:
                    ui.label(day_name).classes(
                        "text-center text-xs text-white font-semibold py-1"
                    )

            cal   = _sun_cal.monthdayscalendar(year, month)
            today = datetime.now(_UK_TZ).date()

            for week in cal:
                with ui.grid(columns=7).classes("w-full gap-1.5 mb-1.5"):
                    for day_num in week:
                        if day_num == 0:
                            ui.element("div").style("min-height:88px")
                            continue
                        d    = date(year, month, day_num)
                        info = day_map.get(d)
                        is_today = (d == today)
                        today_ring = "outline:2px solid #3b82f6;" if is_today else ""

                        mkt = market_map.get(d)
                        mkt_label, mkt_colour = mkt if mkt else ("", "")

                        if info:
                            pnl      = info["pnl"]
                            trades_n = info["trades"]
                            wins     = info["wins"]
                            wr       = wins / trades_n * 100 if trades_n else 0.0
                            is_green = pnl >= 0
                            bg     = "#0d2818" if is_green else "#2d0a0a"
                            border = "#16532c" if is_green else "#7f1d1d"
                            pnl_c  = "#4ade80" if is_green else "#f87171"
                            _cell = ui.column().classes(
                                "p-2 rounded gap-0.5 cursor-pointer hover:brightness-125"
                            ).style(
                                f"background:{bg}; border:1px solid {border}; "
                                f"min-height:88px; {today_ring}"
                            )
                            _cell.on("click", lambda _=None, dd=d: _show_day_detail(dd))
                            with _cell:
                                with ui.row().classes("items-center justify-between w-full"):
                                    ui.label(str(day_num)).classes(
                                        "text-xs text-white font-bold"
                                    )
                                    if trades_n > 0:
                                        ui.label(f"{wr:.0f}%").classes(
                                            "text-xs text-white font-semibold"
                                        )
                                ui.label(_pnl_fmt(pnl)).classes(
                                    "text-sm font-bold font-mono"
                                ).style(f"color:{pnl_c}")
                                ui.label(
                                    f"{trades_n} trade{'s' if trades_n != 1 else ''}"
                                ).classes("text-xs text-white")
                                if mkt_label:
                                    ui.label(mkt_label).classes(
                                        "text-xs font-semibold leading-none mt-0.5"
                                    ).style(f"color:{mkt_colour}")
                        else:
                            with ui.column().classes("p-2 rounded gap-0").style(
                                f"background:#0f1117; border:1px solid #1f2937; "
                                f"min-height:88px; {today_ring}"
                            ):
                                ui.label(str(day_num)).classes(
                                    "text-xs font-bold " +
                                    ("text-blue-300" if is_today else "text-white")
                                )
                                if is_today:
                                    ui.label("Today").classes("text-xs text-blue-400 italic")
                                if mkt_label:
                                    ui.label(mkt_label).classes(
                                        "text-xs font-semibold leading-none mt-1"
                                    ).style(f"color:{mkt_colour}")

            # ── Week summary row (below grid) ─────────────────────────────────
            ui.separator().classes("my-3")
            ui.label("Weekly Totals").classes("text-xs text-white font-semibold mb-2 uppercase tracking-wider")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for wi, week in enumerate(_sun_cal.monthdayscalendar(year, month)):
                    days_in   = [day for day in week if day != 0]
                    if not days_in:
                        continue
                    w_dates   = [date(year, month, d) for d in days_in]
                    w_trading = [d for d in w_dates if d in day_map]
                    w_pnl     = sum(day_map[d]["pnl"] for d in w_trading)
                    w_trades  = sum(day_map[d]["trades"] for d in w_trading)
                    is_green  = w_pnl >= 0
                    col = "text-green-400" if is_green else "text-red-400"
                    bg  = "#0d2818" if is_green else "#2d0a0a"
                    with ui.card().classes("flex-1 min-w-20 rounded p-2").style(
                        f"background:{bg}; border:1px solid {'#16532c' if is_green else '#7f1d1d'}"
                    ):
                        ui.label(f"Week {wi + 1}").classes("text-xs text-white font-semibold")
                        ui.label(_pnl_fmt(w_pnl)).classes(f"text-sm font-bold {col}")
                        ui.label(
                            f"{w_trades} trade{'s' if w_trades != 1 else ''}"
                        ).classes("text-xs text-white")

    # Cache the last-rendered state so background timer reloads skip the
    # expensive clear-and-rebuild when the underlying data hasn't changed.
    _render_cache: dict = {"day_map": None, "market_map": None,
                           "year": None, "month": None}

    async def reload(force: bool = False):
        """Refresh calendar data.  Pass force=True on month navigation so the
        grid always redraws (showing the loading state) even if data is cached."""
        if force:
            stats_lbl.text = "Loading..."
            source_lbl.text = ""

        day_map, market_map, source = await _build_day_map(_state["year"], _state["month"])

        total_pnl    = sum(v["pnl"] for v in day_map.values())
        trading_days = len(day_map)
        total_trades = sum(v["trades"] for v in day_map.values())
        total_wins   = sum(v["wins"]   for v in day_map.values())
        win_rate     = total_wins / total_trades * 100 if total_trades else 0.0

        sign  = "+" if total_pnl >= 0 else ""
        pnl_s = f"${sign}{total_pnl:,.2f}"

        stats_lbl.text = (
            f"Month total: {pnl_s}  |  {trading_days} trading days  |  "
            f"{total_trades} trades  |  {win_rate:.0f}% win rate"
        )
        stats_lbl.classes(
            replace="text-sm font-semibold " +
                    ("text-green-400" if total_pnl >= 0 else "text-red-400")
        )
        source_lbl.text = f"({source})"

        # Only rebuild the DOM grid when data or month has actually changed,
        # preventing the clear→rebuild flicker on every background timer tick.
        cache_key = (_state["year"], _state["month"])
        data_changed = (
            force
            or _render_cache["year"]  != _state["year"]
            or _render_cache["month"] != _state["month"]
            or _render_cache["day_map"] != day_map
        )
        if data_changed:
            _render_cache.update({"day_map": day_map, "market_map": market_map,
                                  "year": _state["year"], "month": _state["month"]})
            _draw_grid(day_map, market_map, _state["year"], _state["month"])

    month_lbl_ref: list = []

    def _update_header():
        if month_lbl_ref:
            month_lbl_ref[0].text = datetime(
                _state["year"], _state["month"], 1
            ).strftime("%B %Y")

    def prev_month():
        if _state["month"] == 1:
            _state["month"], _state["year"] = 12, _state["year"] - 1
        else:
            _state["month"] -= 1
        _update_header()
        asyncio.create_task(reload(force=True))

    def next_month():
        if _state["month"] == 12:
            _state["month"], _state["year"] = 1, _state["year"] + 1
        else:
            _state["month"] += 1
        _update_header()
        asyncio.create_task(reload(force=True))

    def this_month():
        now = datetime.now()
        _state["year"], _state["month"] = now.year, now.month
        _update_header()
        asyncio.create_task(reload(force=True))

    with header_row:
        ui.button(icon="chevron_left",  on_click=prev_month).classes(
            "bg-gray-700 text-white text-xs px-2 py-1"
        )
        lbl = ui.label(
            datetime(_state["year"], _state["month"], 1).strftime("%B %Y")
        ).classes("text-base font-semibold text-gray-200 w-32 text-center")
        month_lbl_ref.append(lbl)
        ui.button(icon="chevron_right", on_click=next_month).classes(
            "bg-gray-700 text-white text-xs px-2 py-1"
        )
        ui.button("This month", on_click=this_month).classes(
            "bg-gray-700 text-white text-xs px-3 py-1 ml-2"
        )
        ui.button(icon="refresh", on_click=lambda: asyncio.create_task(reload(force=True))).classes(
            "bg-blue-700 text-white text-xs px-2 py-1 ml-1"
        ).tooltip("Refresh from MT5")
        ui.space()
        stats_lbl
        source_lbl

    asyncio.ensure_future(reload(force=True))
    ui.timer(15.0, reload)  # silent background poll — only redraws if data changed


# ── Hour × Weekday P&L heat map ─────────────────────────────────────────────────

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
        grid = db_module.get_hourly_pnl_grid(period["days"])
        with container:
            ui.label(
                "Average net P&L by UTC hour × weekday — deep red = hours that consistently "
                "lose (candidates to stop trading), green = consistent winners."
            ).classes("text-xs text-gray-400 mb-1")
            if not grid:
                ui.label("No closed trades in this period.").classes("text-gray-500 italic")
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
                s = db_module._session_for_hour(h)
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
        from backend.src.config import load as _cfg_load
        _analysis_body = _refs["analysis_body"]

        stored = db_module.get_app_config("heatmap_analysis_cache")
        if stored and not force:
            try:
                obj = _json.loads(stored)
                age_h = (_dt.now().timestamp() - obj.get("ts", 0)) / 3600
                if age_h < 20:
                    _render_heatmap_analysis(obj.get("result", {}), obj.get("ts", 0))
                    return
            except Exception:
                pass

        cfg = _cfg_load()

        _analysis_body.clear()
        with _analysis_body:
            with ui.row().classes("items-center gap-2"):
                ui.spinner("dots", size="sm", color="yellow")
                ui.label("Analysing with AI...").classes("text-xs text-gray-400")

        async def _run_analysis():
            _body = _refs["analysis_body"]
            grid  = db_module.get_hourly_pnl_grid(period["days"])
            if not grid:
                _body.clear()
                with _body:
                    ui.label("No trade data for analysis.").classes("text-xs text-gray-500 italic")
                return

            sess_totals: dict = {}
            best_hours:  list = []
            worst_hours: list = []
            for (di, h), c in grid.items():
                s = db_module._session_for_hour(h)
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
                from forex_trader.core import ai_provider as _aip
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
                db_module.set_app_config("heatmap_analysis_cache", _json2.dumps({
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
                except Exception:
                    pass
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


# ── Channel scorecard + adaptive sizing ─────────────────────────────────────────

def _render_channels(engine):
    """Per-channel performance with rolling adaptive lot multiplier and pause control."""
    period    = {"days": 30}
    container = ui.column().classes("w-full gap-2")

    def _draw():
        container.clear()
        newly_paused = db_module.recompute_channel_performance(period["days"])
        for _src in newly_paused:
            asyncio.create_task(telegram_alerts.send_message(
                f"Channel auto-paused: *{_src}*\n"
                "Profit factor dropped below 0.8 over the rolling 30-day window. "
                "The channel will not open new trades until performance recovers or you re-enable it manually.",
            ))
        scorecard = db_module.get_channel_scorecard(period["days"])
        perfmap   = db_module.get_channel_performance_map()
        with container:
            ui.label(
                "Rolling performance by signal source. The lot multiplier scales position "
                "size on risk-based entries; paused channels are blocked from opening trades. "
                "Profit factor < 0.8 over 8+ trades auto-pauses (Bounce Generator excluded — manage manually); "
                "manual pause overrides."
            ).classes("text-xs text-gray-400 mb-2")
            if not scorecard:
                ui.label("No closed trades in this period.").classes("text-gray-500 italic")
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
                                db_module.set_channel_paused(s, not p)
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
