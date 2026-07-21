"""
Breakout signal engine UI panel.
Completely isolated view — shows only breakout signal data, never bounce data.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Optional

from forex_trader.core import database as db_module

from nicegui import ui

from forex_trader.breakout_signal import breakout_signal_repo as _bdb_real
from forex_trader.breakout_signal import breakout_signal_service as bo_engine_module
from forex_trader.breakout_signal import adaptive_params as _ap_real
from forex_trader.breakout_signal import ml_engine as _bo_ml_real
from forex_trader.sync import client as sync_client
from forex_trader.sync.remote_stats_facade import make_facades, _is_remote_active, _is_centralized_remote_mode

# In Remote mode (VPS is the active trader), these transparently read from
# the mirrored remote signal-gen stats instead of this node's own local
# data — see sync/remote_stats_facade.py. Every other call site below is
# unchanged; the facades expose the same functions as the real modules.
bdb, bo_ml, ap = make_facades("breakout", _bdb_real, _bo_ml_real, _ap_real)

_STARTING_BALANCE = 1000.0


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s <= 0:
        return "—"
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h {m % 60}m" if m % 60 else f"{h}h"


def _dir_color(d: str) -> str:
    return "text-green-400" if str(d).upper() == "BUY" else "text-red-400"


def _pnl_color(v) -> str:
    try:
        return "text-green-400" if float(v) >= 0 else "text-red-400"
    except (TypeError, ValueError):
        return "text-gray-400"


def _pnl_str(v, prefix="") -> str:
    try:
        return f"{prefix}{float(v):+.2f}"
    except (TypeError, ValueError):
        return "—"


def _outcome_color(o: str) -> str:
    return {
        "win":  "text-green-400",
        "loss": "text-red-400",
        "be":   "text-yellow-400",
    }.get((o or "").lower(), "text-gray-400")


def _bo_type_badge(btype: str) -> tuple[str, str]:
    """Return (display_text, color) for breakout type."""
    if btype == "go":
        return "BREAK→GO", "bg-orange-700 text-orange-100"
    elif btype == "retest":
        return "RETEST", "bg-blue-700 text-blue-100"
    return btype.upper(), "bg-gray-700 text-gray-300"


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    eng = bo_engine_module.get_instance()

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700"
    ):
        ui.label("trending_up").classes("material-icons text-orange-400 text-xl")
        ui.label("Breakout Engine").classes(
            "text-orange-400 font-bold text-sm tracking-widest"
        )
        from forex_trader.core import database as _cdb_hdr
        _bo_live_hdr = bool(_cdb_hdr.get_risk_settings().get("bo_live_execution", 0))
        exec_lbl = ui.label(
            "• LIVE EXECUTION — MT5 ORDERS ACTIVE •" if _bo_live_hdr
            else "• VIRTUAL — NO MT5 ORDERS •"
        ).classes(
            "text-green-400 text-xs font-mono" if _bo_live_hdr
            else "text-gray-500 text-xs font-mono"
        )
        ui.label("M5 candle gate + 3s velocity monitor").classes(
            "text-gray-600 text-xs italic"
        )
        status_chip = ui.badge("stopped", color="gray").classes("text-xs ml-auto")
        detail_lbl  = ui.label("").classes("text-gray-400 text-xs")

    # ── Balance banner ────────────────────────────────────────────────────────
    balance_row = ui.row().classes(
        "w-full items-center gap-6 px-4 py-3 bg-gray-900 border-b border-gray-700"
    )

    async def _render_balance():
        balance   = await db_module.to_db_thread(bdb.get_virtual_balance)
        pnl_total = round(balance - _STARTING_BALANCE, 2)
        pnl_pct   = round(pnl_total / _STARTING_BALANCE * 100, 1)
        max_dd    = await db_module.to_db_thread(bdb.get_max_drawdown)
        stats     = await db_module.to_db_thread(bdb.get_stats)
        balance_row.clear()
        with balance_row:
            bal_color = "text-green-400" if balance >= _STARTING_BALANCE else "text-red-400"
            pnl_color = "text-green-400" if pnl_total >= 0 else "text-red-400"

            with ui.column().classes("items-center"):
                ui.label(f"${balance:,.2f}").classes(f"text-2xl font-bold {bal_color}")
                ui.label("Virtual Balance").classes("text-xs text-gray-500")

            ui.separator().props("vertical").classes("h-10 border-gray-700")

            with ui.column().classes("items-center"):
                ui.label(f"${_STARTING_BALANCE:,.0f}").classes("text-sm text-gray-400")
                ui.label("Starting").classes("text-xs text-gray-600")

            with ui.column().classes("items-center"):
                ui.label(_pnl_str(pnl_total, "$")).classes(f"text-lg font-bold {pnl_color}")
                ui.label(f"Total P&L ({pnl_pct:+.1f}%)").classes("text-xs text-gray-500")

            ui.separator().props("vertical").classes("h-10 border-gray-700")

            with ui.column().classes("items-center"):
                ui.label(f"${max_dd:,.2f}").classes("text-sm text-orange-400 font-mono")
                ui.label("Max Drawdown").classes("text-xs text-gray-600")

            with ui.column().classes("items-center"):
                ui.label(f"{stats['win_rate']}%").classes("text-sm text-blue-300 font-mono")
                ui.label(f"Win Rate ({stats['wins']}W / {stats['losses']}L)").classes(
                    "text-xs text-gray-600"
                )


    asyncio.create_task(_render_balance())

    # ── Controls ──────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 border-b border-gray-700"
    ):
        start_btn   = ui.button("Start Engine", icon="play_arrow").classes(
            "bg-green-700 hover:bg-green-600 text-white text-xs"
        )
        stop_btn    = ui.button("Stop Engine",  icon="stop").classes(
            "bg-red-800 hover:bg-red-700 text-white text-xs"
        )
        run_now_btn = ui.button("Run Now",      icon="refresh").classes(
            "bg-blue-800 hover:bg-blue-700 text-white text-xs"
        )

    # ── Stats row ─────────────────────────────────────────────────────────────
    stats_row = ui.row().classes("w-full gap-3 flex-wrap px-4 pt-3")

    async def _render_stats():
        stats   = await db_module.to_db_thread(bdb.get_stats)
        balance = await db_module.to_db_thread(bdb.get_virtual_balance)
        pnl_tot = round(balance - _STARTING_BALANCE, 2)
        stats_row.clear()
        with stats_row:
            for label, value, color, tip in [
                ("Total Signals",  str(stats["total"]),                 "text-gray-200",
                 "Total breakout signals generated"),
                ("Wins",           str(stats["wins"]),                  "text-green-400",
                 "Signals that hit TP1 or better"),
                ("Losses",         str(stats["losses"]),                "text-red-400",
                 "Signals that hit stop-loss"),
                ("B/E",            str(stats["be"]),                    "text-yellow-400",
                 "Break-even closes (SL moved to entry)"),
                ("Win Rate",       f"{stats['win_rate']}%",             "text-blue-300",
                 "Wins as % of all closed signals"),
                ("Pending",        str(stats["pending"]),               "text-gray-400",
                 "Signals not yet triggered"),
                ("Avg P&L ($)",    f"${stats['avg_pnl_dollars']:+.2f}", _pnl_color(stats["avg_pnl_dollars"]),
                 "Average dollar P&L per closed trade"),
                ("Total P&L ($)",  f"${pnl_tot:+.2f}",                 _pnl_color(pnl_tot),
                 "Cumulative virtual P&L"),
            ]:
                with ui.card().classes(
                    "bg-gray-800 rounded-lg px-3 py-2 text-center flex-none"
                    " w-32 min-h-[4.5rem] flex flex-col items-center justify-center"
                ):
                    ui.label(value).classes(f"text-lg font-bold {color}")
                    ui.label(label).classes("text-xs text-gray-500 leading-tight mt-0.5")
                    ui.tooltip(tip)

    asyncio.create_task(_render_stats())

    # ── Main content ──────────────────────────────────────────────────────────
    with ui.row().classes("w-full flex-1 gap-0"):

        # ── Left: active + history ────────────────────────────────────────────
        with ui.column().classes("flex-1 min-w-0 p-4 gap-4"):

            # Active positions
            ui.label("Active Positions").classes(
                "text-sm font-semibold text-orange-300 uppercase tracking-wider"
            )
            active_area = ui.column().classes("w-full gap-2")

            async def _render_active():
                sigs = await db_module.to_db_thread(bdb.get_open_signals)
                active_area.clear()
                with active_area:
                    if not sigs:
                        ui.label("No active breakout positions").classes(
                            "text-gray-600 text-sm italic"
                        )
                        return
                    for sig in sigs:
                        direction = sig.get("direction", "?")
                        btype     = sig.get("breakout_type", "go")
                        entry     = float(sig.get("entry_mid", 0) or 0)
                        sl        = float(sig.get("stop_loss", 0) or 0)
                        tp1       = sig.get("tp1")
                        tp3       = sig.get("tp3")
                        rr        = sig.get("rr_tp1") or 0
                        adx       = sig.get("adx_at_signal") or 0
                        bias      = sig.get("htf_bias") or "?"
                        h4b       = sig.get("h4_bias") or "?"
                        session   = sig.get("session") or "?"
                        status    = sig.get("status") or "pending"
                        quality   = float(sig.get("quality_score") or 0)
                        rationale = sig.get("rationale") or ""
                        sl_moved  = bool(sig.get("sl_moved_to_be"))
                        level     = sig.get("broken_level")
                        ltype     = sig.get("broken_level_type") or "level"
                        sig_ref   = sig.get("signal_ref") or f"BO-{sig['id']:04d}"

                        border = "border-green-800" if direction == "BUY" else "border-red-800"
                        badge_text, badge_cls = _bo_type_badge(btype)

                        with ui.card().classes(
                            f"w-full bg-gray-800 rounded-lg p-4 border {border}"
                        ):
                            with ui.row().classes("w-full items-start gap-4"):
                                dir_bg = "bg-green-800" if direction == "BUY" else "bg-red-900"
                                with ui.column().classes(
                                    f"rounded-lg px-3 py-2 {dir_bg} items-center min-w-16"
                                ):
                                    ui.label(direction).classes(
                                        f"text-sm font-bold {_dir_color(direction)}"
                                    )
                                    ui.label("XAUUSD").classes("text-xs text-gray-400")

                                with ui.column().classes("flex-1 gap-1"):
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.element("span").classes(
                                            f"text-xs font-semibold px-1.5 py-0.5 rounded {badge_cls}"
                                        ).text = badge_text
                                        ui.label(f"${entry:.2f}").classes("text-white font-semibold")
                                        sl_label = f"SL ${sl:.2f}" + (" (moved)" if sl_moved else "")
                                        ui.label(sl_label).classes("text-red-300 text-xs")
                                        if tp1:
                                            ui.label(f"TP1 ${float(tp1):.2f}").classes("text-green-300 text-xs")
                                        if tp3:
                                            ui.label(f"TP3 ${float(tp3):.2f}").classes("text-green-400 text-xs")
                                        ui.label(f"R:R {float(rr):.1f}:1").classes("text-blue-300 text-xs font-mono")

                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        if level:
                                            ui.label(f"Break: ${float(level):.2f} ({ltype})").classes("text-orange-300 text-xs")
                                        ui.label(f"ADX {adx:.1f}").classes("text-purple-300 text-xs font-mono")
                                        ui.label(f"H1: {bias}").classes("text-gray-400 text-xs")
                                        ui.label(f"H4: {h4b}").classes("text-gray-400 text-xs")
                                        ui.label(f"Session: {session}").classes("text-gray-400 text-xs")
                                        ui.label(f"Q: {quality:.0%}").classes("text-gray-400 text-xs")

                                    if rationale:
                                        ui.label(f'"{rationale}"').classes(
                                            "text-gray-400 text-xs italic mt-1"
                                        )

                                with ui.column().classes("items-end gap-1 shrink-0"):
                                    ui.label(sig_ref).classes("text-xs font-mono text-gray-500")
                                    ui.label(status.upper()).classes("text-xs font-mono text-blue-300")

            asyncio.create_task(_render_active())

            # Signal history
            ui.label("Signal History").classes(
                "text-sm font-semibold text-gray-300 uppercase tracking-wider mt-4"
            )
            history_area = ui.column().classes("w-full")

            async def _render_history():
                sigs   = await db_module.to_db_thread(bdb.get_all_signals, limit=80)
                closed = [s for s in sigs if s.get("status") not in ("pending", "triggered")]
                history_area.clear()
                with history_area:
                    if not closed:
                        ui.label("No completed breakout signals yet").classes(
                            "text-gray-600 text-sm italic"
                        )
                        return

                    with ui.element("table").classes("w-full text-xs"):
                        with ui.element("thead"):
                            with ui.element("tr").classes(
                                "text-gray-500 border-b border-gray-700"
                            ):
                                for hdr in [
                                    "Ref", "Live Trade", "Date", "Dir", "Type",
                                    "Level", "Entry", "SL", "TP1", "TP3",
                                    "R:R", "ADX", "H1 Bias", "Strategy", "Outcome",
                                    "Held", "PnL pts", "PnL $",
                                ]:
                                    with ui.element("th").classes("text-left px-2 py-1 font-medium"):
                                        ui.label(hdr)

                        with ui.element("tbody"):
                            for sig in closed[:60]:
                                direction   = sig.get("direction", "?")
                                outcome     = sig.get("outcome") or "?"
                                btype       = sig.get("breakout_type", "go")
                                badge_text, _ = _bo_type_badge(btype)
                                pnl_pts     = sig.get("pnl_pts")
                                pnl_dol     = sig.get("pnl_dollars")
                                t_trig      = float(sig.get("trigger_time") or 0)
                                t_close     = float(sig.get("close_time")   or 0)
                                held        = _fmt_dur(t_close - t_trig) if (t_trig and t_close) else "—"
                                adx_v       = sig.get("adx_at_signal")
                                sig_ref     = sig.get("signal_ref") or f"BO-{sig['id']:04d}"
                                mt5_tkt     = sig.get("mt5_ticket")
                                exec_status = sig.get("live_exec_status") or ""

                                live_reason = ""
                                if ":" in exec_status:
                                    live_reason = exec_status.split(":", 1)[1].strip()
                                if mt5_tkt:
                                    live_cell = f"MT5 #{mt5_tkt}"
                                    live_cls  = "text-green-400 font-mono font-bold"
                                elif exec_status.startswith("failed") and "circuit breaker" in exec_status.lower():
                                    live_cell = "CIRCUIT BREAKER"
                                    live_cls  = "text-orange-400 font-mono"
                                elif exec_status.startswith("failed"):
                                    live_cell = f"LIVE FAIL: {live_reason[:40]}" if live_reason else "LIVE FAIL"
                                    live_cls  = "text-red-400 font-mono"
                                elif exec_status.startswith("skipped:ml"):
                                    live_cell = "ML SKIP"
                                    live_cls  = "text-yellow-500 font-mono"
                                elif exec_status.startswith("skipped"):
                                    live_cell = "VIRTUAL"
                                    live_cls  = "text-gray-600 font-mono"
                                else:
                                    live_cell = "VIRTUAL"
                                    live_cls  = "text-gray-600 font-mono"

                                with ui.element("tr").classes(
                                    "border-b border-gray-800 hover:bg-gray-800"
                                ):
                                    sig_strategy = (sig.get("strategy") or "—").replace("_", " ")
                                    for val, cls in [
                                        (sig_ref,                             "text-gray-500 font-mono"),
                                        (live_cell,                           live_cls),
                                        (_fmt_ts(sig.get("created_at")),      "text-gray-400"),
                                        (direction,                           f"{_dir_color(direction)} font-bold"),
                                        (badge_text,                          "text-orange-300 font-mono text-xs"),
                                        (f"${float(sig.get('broken_level') or 0):.2f}",  "text-orange-200"),
                                        (f"${float(sig.get('entry_mid')    or 0):.2f}",  "text-gray-200"),
                                        (f"${float(sig.get('stop_loss')    or 0):.2f}",  "text-red-300"),
                                        (f"${float(sig.get('tp1') or 0):.2f}" if sig.get("tp1") else "—", "text-green-300"),
                                        (f"${float(sig.get('tp3') or 0):.2f}" if sig.get("tp3") else "—", "text-green-400"),
                                        (f"{float(sig.get('rr_tp1') or 0):.1f}:1",       "text-blue-300"),
                                        (f"{float(adx_v):.1f}" if adx_v else "—",        "text-purple-300 font-mono"),
                                        (sig.get("htf_bias") or "—",                     "text-gray-400"),
                                        (sig_strategy,                                    "text-indigo-300 font-mono text-xs"),
                                        (outcome.upper(),                      f"{_outcome_color(outcome)} font-semibold"),
                                        (held,                                 "text-cyan-300 font-mono"),
                                        (_pnl_str(pnl_pts),                   _pnl_color(pnl_pts)),
                                        (_pnl_str(pnl_dol, "$"),              _pnl_color(pnl_dol) + " font-semibold"),
                                    ]:
                                        with ui.element("td").classes(f"px-2 py-1 {cls}"):
                                            lbl = ui.label(val)
                                            if val is live_cell and live_reason:
                                                lbl.tooltip(live_reason)

            asyncio.create_task(_render_history())

        # ── Right: analytics + log ────────────────────────────────────────────
        with ui.column().classes("w-96 shrink-0 border-l border-gray-700 p-4 gap-5"):

            # ── Performance analytics ─────────────────────────────────────────
            ui.label("Performance Analytics").classes(
                "text-sm font-semibold text-orange-300 uppercase tracking-wider"
            )
            analytics_area = ui.column().classes("w-full gap-3")

            async def _render_analytics():
                by_type    = await db_module.to_db_thread(bdb.get_perf_by_breakout_type)
                by_adx     = await db_module.to_db_thread(bdb.get_perf_by_adx_band)
                by_session = await db_module.to_db_thread(bdb.get_perf_by_session)
                by_bias    = await db_module.to_db_thread(bdb.get_perf_by_bias)
                analytics_area.clear()
                with analytics_area:

                    def _perf_table(title: str, rows: list[dict], key_col: str):
                        if not rows:
                            return
                        ui.label(title).classes(
                            "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1"
                        )
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-500 border-b border-gray-700"):
                                    for h in [key_col.replace("_", " ").title(), "W", "L", "Avg $", "Total $"]:
                                        ui.element("th").classes("text-left px-1 py-0.5").text = h
                            with ui.element("tbody"):
                                for r in rows:
                                    total_pnl = float(r.get("total_pnl") or 0)
                                    avg_pnl   = float(r.get("avg_pnl")   or 0)
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        for val, cls in [
                                            (str(r.get(key_col) or "?"), "text-gray-300"),
                                            (str(r.get("wins",   0)),    "text-green-400"),
                                            (str(r.get("losses", 0)),    "text-red-400"),
                                            (f"${avg_pnl:+.2f}",         _pnl_color(avg_pnl)),
                                            (f"${total_pnl:+.2f}",       _pnl_color(total_pnl) + " font-semibold"),
                                        ]:
                                            with ui.element("td").classes(f"px-1 py-0.5 {cls}"):
                                                ui.label(val)

                    _perf_table("By Entry Type",   by_type,    "breakout_type")
                    _perf_table("By ADX Band",     by_adx,     "adx_band")
                    _perf_table("By Session",      by_session, "session")
                    _perf_table("By HTF Bias",     by_bias,    "htf_bias")

            asyncio.create_task(_render_analytics())

            ui.separator().classes("border-gray-700 my-1")

            # ── Adaptive parameters ───────────────────────────────────────────
            ui.label("Engine Parameters").classes(
                "text-sm font-semibold text-purple-300 uppercase tracking-wider"
            )
            ap_area = ui.column().classes("w-full gap-1")

            async def _render_ap():
                all_p = await db_module.to_db_thread(ap.get_all)
                ap_area.clear()
                with ap_area:
                    with ui.element("table").classes("w-full text-xs"):
                        with ui.element("thead"):
                            with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                for h in ["Parameter", "Current", "Default"]:
                                    ui.element("th").classes("text-left px-1 py-0.5").text = h
                        with ui.element("tbody"):
                            for key, info in all_p.items():
                                cur  = info["value"]
                                dflt = info["default"]
                                changed = abs(cur - dflt) > 1e-5
                                label   = key.replace("_", " ")
                                cur_str  = f"{cur:.2f}" if cur != int(cur) else str(int(cur))
                                dflt_str = f"{dflt:.2f}" if dflt != int(dflt) else str(int(dflt))
                                with ui.element("tr").classes("border-b border-gray-800"):
                                    with ui.element("td").classes("px-1 py-0.5 text-gray-400"):
                                        ui.label(label).tooltip(info["desc"])
                                    with ui.element("td").classes(
                                        f"px-1 py-0.5 font-mono font-semibold "
                                        f"{'text-purple-300' if changed else 'text-gray-300'}"
                                    ):
                                        ui.label(cur_str + (" *" if changed else ""))
                                    with ui.element("td").classes("px-1 py-0.5 text-gray-600 font-mono"):
                                        ui.label(dflt_str)

                    def _reset_ap():
                        ap.reset_to_defaults()
                        asyncio.create_task(_render_ap())
                        ui.notify("Breakout parameters reset", type="info")

                    ui.button("Reset to Defaults", on_click=_reset_ap).classes(
                        "bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs px-2 py-1 mt-1"
                    )

            asyncio.create_task(_render_ap())

            ui.separator().classes("border-gray-700 my-1")

            # ── ML Learning ───────────────────────────────────────────────────
            ui.label("ML Learning").classes(
                "text-sm font-semibold text-purple-300 uppercase tracking-wider"
            )
            ml_area = ui.column().classes("w-full gap-2")

            async def _render_ml():
                s    = await db_module.to_db_thread(bo_ml.summary)
                mets = await db_module.to_db_thread(bo_ml.get_ml_metrics)
                ml_area.clear()
                with ml_area:

                    # ── Scorecard chips ────────────────────────────────────────
                    with ui.row().classes("flex-wrap gap-2"):
                        def _chip(label: str, value: str, color: str, tip: str):
                            with ui.card().classes(
                                "bg-gray-800 rounded px-2 py-1 text-center min-w-16"
                            ):
                                ui.label(value).classes(f"text-sm font-bold {color} font-mono")
                                ui.label(label).classes("text-xs text-gray-500")
                                ui.tooltip(tip)

                        pred_r_val = mets.get("mean_pred_r")
                        pred_r_str = f"{pred_r_val:+.3f}" if pred_r_val is not None else "—"
                        pred_r_col = (
                            "text-green-400"  if pred_r_val is not None and pred_r_val > 0.3 else
                            "text-yellow-400" if pred_r_val is not None and pred_r_val > 0.0 else
                            "text-red-400"
                        ) if pred_r_val is not None else "text-gray-500"
                        _chip("Pred R", pred_r_str, pred_r_col,
                              "Mean predicted R-multiple across all closed signals. "
                              "Model is gating on R>0; target >0.3 = model is confident.")

                        act_r_val = mets.get("mean_actual_r")
                        act_r_str = f"{act_r_val:+.3f}" if act_r_val is not None else "—"
                        act_r_col = (
                            "text-green-400"  if act_r_val is not None and act_r_val > 0.0 else
                            "text-yellow-400" if act_r_val is not None and act_r_val > -0.3 else
                            "text-red-400"
                        ) if act_r_val is not None else "text-gray-500"
                        _chip("Act R", act_r_str, act_r_col,
                              "Mean actual R-multiple across closed signals (+1=win, -1=loss, 0=BE). "
                              "Target >0 = edge is positive.")

                        _chip("Labeled", str(mets.get("n_data", 0)), "text-blue-300",
                              "Closed signals with ML probability stored.")

                        _chip("Samples", str(s.get("labeled_count", 0)), "text-gray-300",
                              "Total labeled training examples (closed signals with features).")

                        needed   = bo_ml.MIN_TRAIN_SAMPLES
                        have     = s.get("labeled_count", 0)
                        next_in  = max(0, needed - have) if not s.get("trained") else \
                                   bo_ml.RETRAIN_EVERY - (have % bo_ml.RETRAIN_EVERY or bo_ml.RETRAIN_EVERY)
                        next_str = f"+{next_in}" if s.get("trained") else f"{have}/{needed}"
                        _chip("Next Train", next_str, "text-cyan-300",
                              f"Retrains every {bo_ml.RETRAIN_EVERY} new labeled examples "
                              f"once {bo_ml.MIN_TRAIN_SAMPLES} minimum reached.")

                        backend = "LGB" if s.get("lgb_available") else "RF"
                        _chip("Backend", backend, "text-orange-300",
                              f"ML backend: {'LightGBM' if s.get('lgb_available') else 'RandomForest'}. "
                              f"Trained: {'yes' if s.get('trained') else 'no'}.")

                    # ── Is it learning? ────────────────────────────────────────
                    sig_ids      = mets.get("signal_ids", [])
                    win_rates    = mets.get("win_rate_series", [])
                    pred_r_ser   = mets.get("pred_r_series", [])
                    actual_r_ser = mets.get("actual_r_series", [])

                    if sig_ids:
                        ui.label("Is it learning?").classes(
                            "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1"
                        )
                        n = len(sig_ids)
                        W, H = 280, 50

                        def _to_svg_points(series: list, lo: float, hi: float,
                                           w: int, h: int) -> str:
                            if not series or hi == lo:
                                return ""
                            pts = []
                            for i, v in enumerate(series):
                                if v is None:
                                    continue
                                x = int(i / max(len(series) - 1, 1) * w)
                                y = int(h - (v - lo) / (hi - lo) * h)
                                pts.append(f"{x},{y}")
                            return " ".join(pts)

                        wr_pts = _to_svg_points(win_rates, 0, 100, W, H)
                        cum_r: list = []
                        running = 0.0
                        for v in actual_r_ser:
                            running += v
                            cum_r.append(round(running / len(cum_r + [0]), 3))
                        ar_pts = _to_svg_points(cum_r, -1.0, 1.0, W, H)

                        svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
                                   xmlns="http://www.w3.org/2000/svg"
                                   style="background:#1f2937;border-radius:4px">
                          <line x1="0" y1="{H//2}" x2="{W}" y2="{H//2}"
                                stroke="#374151" stroke-width="1" stroke-dasharray="4,4"/>
                          {f'<polyline points="{wr_pts}" fill="none" stroke="#4ade80" stroke-width="1.5"/>' if wr_pts else ''}
                          {f'<polyline points="{ar_pts}" fill="none" stroke="#fb923c" stroke-width="1.5" stroke-dasharray="3,2"/>' if ar_pts else ''}
                        </svg>"""
                        ui.html(svg).tooltip(
                            "Green = cumulative win rate (target >50%). "
                            "Orange dashed = mean actual R-multiple (target >0)."
                        )
                        with ui.row().classes("gap-3 text-xs"):
                            ui.label("— win rate").classes("text-green-400")
                            ui.label("--- actual R").classes("text-orange-400")

                        last_n = min(5, n)
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                    for h_label in ["Signal", "Win%", "Pred R", "Act R"]:
                                        ui.element("th").classes("text-left px-1 py-0.5").text = h_label
                            with ui.element("tbody"):
                                for i in range(n - last_n, n):
                                    _pr = pred_r_ser[i] if i < len(pred_r_ser) else None
                                    _ar = actual_r_ser[i] if i < len(actual_r_ser) else None
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        cells = [
                                            (str(sig_ids[i])[:14],
                                             "text-gray-500 font-mono"),
                                            (f"{win_rates[i]:.0f}%" if i < len(win_rates) else "—",
                                             "text-green-400 font-mono"),
                                            (f"{_pr:+.3f}" if _pr is not None else "—",
                                             "text-orange-300 font-mono"),
                                            (f"{_ar:+.1f}" if _ar is not None else "—",
                                             "text-purple-300 font-mono"),
                                        ]
                                        for v, c in cells:
                                            with ui.element("td").classes(f"px-1 py-0.5 {c}"):
                                                ui.label(v)
                    else:
                        ui.label(
                            f"No calibration data yet. "
                            f"Need {bo_ml.MIN_TRAIN_SAMPLES} closed signals with ML probability stored."
                        ).classes("text-gray-600 text-xs italic")

                    # ── Calibration ────────────────────────────────────────────
                    calib = mets.get("calibration", [])
                    if any(c["count"] > 0 for c in calib):
                        ui.label("Calibration").classes(
                            "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-2"
                        ).tooltip(
                            "How well ML probabilities match actual win rates. "
                            "Perfect calibration = predicted% equals actual%. "
                            "Above diagonal = overconfident; below = underconfident."
                        )
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                    for h_lbl, tip in [
                                        ("Bin",       "Predicted probability range"),
                                        ("Predicted", "Mean ML predicted win probability"),
                                        ("Actual",    "Actual win rate in this bin"),
                                        ("n",         "Number of signals in this bin"),
                                        ("Drift",     "Deviation from perfect calibration"),
                                    ]:
                                        with ui.element("th").classes("text-left px-1 py-0.5"):
                                            ui.label(h_lbl).classes(
                                                "cursor-help underline decoration-dotted decoration-gray-700"
                                            ).tooltip(tip)
                            with ui.element("tbody"):
                                for c in calib:
                                    actual = c["actual_win_pct"]
                                    pred   = c["predicted_pct"]
                                    if actual is None:
                                        drift_str  = "—"
                                        drift_col  = "text-gray-600"
                                        actual_str = "—"
                                        actual_col = "text-gray-600"
                                    else:
                                        drift = actual - pred
                                        drift_str = f"{drift:+.0f}%"
                                        drift_col = (
                                            "text-green-400"  if abs(drift) < 10 else
                                            "text-yellow-400" if abs(drift) < 20 else
                                            "text-red-400"
                                        )
                                        actual_str = f"{actual:.0f}%"
                                        actual_col = "text-blue-300"
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        for val, cls in [
                                            (c["label"],      "text-gray-400"),
                                            (f"{pred:.0f}%",  "text-purple-300 font-mono"),
                                            (actual_str,      actual_col + " font-mono"),
                                            (str(c["count"]), "text-gray-500"),
                                            (drift_str,       drift_col + " font-mono"),
                                        ]:
                                            with ui.element("td").classes(f"px-1 py-0.5 {cls}"):
                                                ui.label(val)

                    # ── Feature importance (last retrain) ──────────────────────
                    th = mets.get("train_history", [])
                    if th:
                        last_retrain = th[-1]
                        fi = last_retrain.get("feature_importances", {})
                        if fi:
                            top5 = sorted(fi.items(), key=lambda x: -x[1])[:5]
                            max_imp = top5[0][1] if top5 else 1.0
                            ui.label("Top Features (last retrain)").classes(
                                "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-2"
                            ).tooltip(
                                f"LightGBM feature importances from most recent retrain "
                                f"({last_retrain['n_samples']} samples). "
                                "Longer bar = more influence on the model's prediction."
                            )
                            for fname, imp in top5:
                                bar_w = int(imp / max_imp * 120) if max_imp > 0 else 0
                                with ui.row().classes("items-center gap-1"):
                                    ui.label(fname.replace("_", " ")).classes(
                                        "text-gray-400 text-xs w-28 shrink-0"
                                    )
                                    ui.html(
                                        f'<div style="width:{bar_w}px;height:6px;'
                                        f'background:#7c3aed;border-radius:3px"></div>'
                                    )

            asyncio.create_task(_render_ml())

            ui.separator().classes("border-gray-700 my-1")

            # ── Cycle log ─────────────────────────────────────────────────────
            ui.label("Cycle Log").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            log_area = ui.column().classes("w-full gap-2 overflow-y-auto").style(
                "max-height:50vh"
            )

            async def _render_log():
                entries = await db_module.to_db_thread(bdb.get_analysis_log, limit=40)
                log_area.clear()
                with log_area:
                    if not entries:
                        ui.label("No cycles run yet").classes("text-gray-600 text-xs italic")
                        return
                    for entry in entries:
                        ts        = _fmt_ts(entry.get("ts"))
                        session   = entry.get("session") or "?"
                        bias      = entry.get("htf_bias") or "?"
                        h4b       = entry.get("h4_bias") or "?"
                        price     = entry.get("price") or 0
                        adx_v     = entry.get("adx")
                        result    = entry.get("result") or ""
                        reason    = entry.get("suppressed_reason") or ""
                        claude    = entry.get("claude_decision") or ""
                        candidate = entry.get("candidate") or {}
                        trigger   = candidate.get("trigger", "M5") if isinstance(candidate, dict) else "M5"

                        is_signal  = result.startswith("signal_created")
                        is_velocity = trigger == "velocity"
                        border = "border-orange-500" if (is_signal and is_velocity) else \
                                 "border-orange-800" if is_signal else "border-gray-700"

                        with ui.card().classes(
                            f"w-full rounded p-2 bg-gray-800 border {border}"
                        ):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(ts).classes("text-gray-500 text-xs font-mono")
                                ui.label(f"{session} | H1:{bias} H4:{h4b}").classes(
                                    "text-gray-400 text-xs"
                                )
                            if price:
                                adx_str = f"  ADX {float(adx_v):.1f}" if adx_v else ""
                                ui.label(f"${float(price):.2f}{adx_str}").classes(
                                    "text-gray-300 text-xs font-mono"
                                )
                            if is_signal:
                                trigger_badge = "⚡ VELOCITY" if is_velocity else "📊 M5 CANDLE"
                                ui.label(f"BREAKOUT SIGNAL  {trigger_badge}").classes(
                                    "text-orange-400 text-xs font-bold"
                                )
                            elif reason:
                                ui.label(reason).classes("text-gray-500 text-xs italic leading-tight")
                            if claude:
                                ui.label(f'"{claude}"').classes(
                                    "text-blue-300 text-xs italic leading-tight"
                                )

            asyncio.create_task(_render_log())

    # ── Refresh orchestrator ──────────────────────────────────────────────────

    async def _refresh_all():
        try:
            await _render_balance()
            await _render_stats()
            await _render_active()
            await _render_history()
            await _render_analytics()
            await _render_ap()
            await _render_ml()
            await _render_log()

            # Update live/virtual execution label in header
            from forex_trader.core import database as _cdb_ref
            _rs_live = await _cdb_ref.to_db_thread(_cdb_ref.get_risk_settings)
            _live_now = bool(_rs_live.get("bo_live_execution", 0))
            exec_lbl.set_text(
                "• LIVE EXECUTION — MT5 ORDERS ACTIVE •" if _live_now
                else "• VIRTUAL — NO MT5 ORDERS •"
            )
            exec_lbl.classes(
                remove="text-gray-500 text-green-400",
                add="text-green-400" if _live_now else "text-gray-500",
            )

            # In Remote mode this is the VPS's own engine state (mirrored
            # every 3s via the sync heartbeat), NOT this node's local
            # instance, which is stood down and would always show "stopped"
            # here even while the VPS is actively running it.
            if _is_remote_active():
                cli = sync_client.get_instance()
                is_r = bool((cli.remote_status.get("engines", {}) if cli else {}).get("breakout"))
                status_chip.set_text("RUNNING - REMOTE" if is_r else "STOPPED - REMOTE")
                status_chip.props(f"color={'orange' if is_r else 'gray'}")
                detail_lbl.set_text("")
            elif eng:
                chip_text  = eng.status.upper() if eng.is_running else "stopped"
                if _is_centralized_remote_mode():
                    chip_text += " - LOCAL"
                chip_color = "orange" if eng.is_running else "gray"
                status_chip.set_text(chip_text)
                status_chip.props(f"color={chip_color}")
                if eng.last_cycle_at:
                    detail_lbl.set_text(
                        f"Last: {_fmt_ts(eng.last_cycle_at)}  {eng.status_detail or ''}"
                    )
        except Exception:
            pass

    # ── Control handlers ──────────────────────────────────────────────────────

    async def _remote_control(action: str, success_msg: str) -> None:
        """Send Start/Stop/Run Now to the VPS's own breakout engine instead
        of this node's local one, which is stood down in Remote mode and
        would do nothing while the button looked like it worked."""
        cli = sync_client.get_instance()
        if cli is None:
            ui.notify("Not connected to VPS", type="negative")
            return
        try:
            ack = await cli.send_engine_control("breakout", action)
            if ack.get("error"):
                ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
            else:
                ui.notify(success_msg, type="positive" if action != "stop" else "warning")
        except Exception as e:
            ui.notify(f"Failed to reach VPS: {e}", type="negative")

    async def _on_start():
        if _is_remote_active():
            await _remote_control("start", "Breakout engine started (VPS)")
            return
        if eng:
            bdb.set_config("bo_engine_enabled", "1")
            eng.start()
            await _refresh_all()
            ui.notify("Breakout engine started", type="positive")

    async def _on_stop():
        if _is_remote_active():
            await _remote_control("stop", "Breakout engine stopped (VPS)")
            return
        if eng:
            bdb.set_config("bo_engine_enabled", "0")
            eng.stop()
            await _refresh_all()
            ui.notify("Breakout engine stopped", type="warning")

    async def _on_run_now():
        if _is_remote_active():
            await _remote_control("run_now", "Manual cycle triggered (VPS)")
            return
        if eng and eng.is_running:
            await eng._run_cycle()
            await _refresh_all()
            ui.notify("Manual cycle complete", type="info")
        elif eng:
            ui.notify("Start the engine first", type="negative")

    start_btn.on("click",   _on_start)
    stop_btn.on("click",    _on_stop)
    run_now_btn.on("click", _on_run_now)

    def _safe_refresh():
        # The engine's own refresh callback is invoked synchronously
        # (fire-and-forget from its perspective), and _refresh_all is now a
        # coroutine function (its DB reads are offloaded to a worker thread)
        # — schedule it as a task rather than calling it directly, which
        # would silently create-and-drop the coroutine without running it.
        async def _run():
            try:
                await _refresh_all()
            except RuntimeError:
                # NiceGUI: parent slot deleted after page disconnect — ignore
                pass
        asyncio.create_task(_run())

    if eng:
        eng.add_refresh_callback(_safe_refresh)
        asyncio.create_task(_refresh_all())

    ui.timer(30, _safe_refresh)
