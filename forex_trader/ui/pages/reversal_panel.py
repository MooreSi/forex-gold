"""
Reversal Engine panel — UI tab inside Signal Generator.

Shows:
  - Engine controls (Start/Stop/Run Now)
  - Balance / P&L banner
  - Correlation scorecard (vs actual reference-channel signals)
  - Active levels (candidate S/R currently tracked)
  - Open signals (pending/triggered)
  - Signal history with correlation indicators
  - ML status (learning progress)
  - Cycle analysis log
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Callable, Optional

from forex_trader.core import database as db_module

from nicegui import ui

from forex_trader.reversal_engine import reversal_engine_repo as _re_db_real
from forex_trader.reversal_engine import reversal_engine_service as re_engine_module
from forex_trader.reversal_engine import ml_engine as _re_ml_real
from forex_trader.sync import client as sync_client
from forex_trader.sync.remote_stats_facade import make_facades, _is_remote_active, _is_centralized_remote_mode

# In Remote mode (VPS is the active trader), these transparently read from
# the mirrored remote signal-gen stats instead of this node's own local
# data — see sync/remote_stats_facade.py. Every other call site below is
# unchanged; the facades expose the same functions as the real modules.
re_db, re_ml, _ = make_facades("reversal_engine", _re_db_real, _re_ml_real)

_STARTING_BALANCE = 1000.0


def _get_realised_pnl() -> dict:
    """What this engine's trades actually made, from the core trade ledger.

    Deliberately not read from reversal_engine.db: that database records
    every signal the generator produced and prices them all at the virtual
    lot, whether or not the trade was ever placed. Only rows here in
    vantage_simulated_trades correspond to orders that really went to MT5.
    """
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(net_pnl), 0) total "
            "FROM vantage_simulated_trades "
            "WHERE status='closed' AND tg_source='Reversal Engine'"
        ).fetchone()
    n = int(row[0] or 0)
    total = float(row[1] or 0.0)
    return {"n": n, "total": total, "per_trade": (total / n) if n else 0.0}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_duration(seconds: float) -> str:
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


def _dir_color(direction: str) -> str:
    return "text-green-400" if str(direction).upper() == "BUY" else "text-red-400"


def _outcome_color(outcome: str) -> str:
    return {
        "win":     "text-green-400",
        "loss":    "text-red-400",
        "be":      "text-yellow-400",
        "open":    "text-gray-400",
        "expired": "text-gray-600",
    }.get((outcome or "").lower(), "text-gray-400")


def _pnl_str(val, prefix: str = "") -> str:
    if val is None:
        return "—"
    return f"{prefix}{float(val):+.2f}"


def _pnl_color(val) -> str:
    if val is None:
        return "text-gray-400"
    return "text-green-400" if float(val) >= 0 else "text-red-400"


def _live_exec_badge(exec_st: str) -> Optional[tuple[str, str, str]]:
    """(badge_text, badge_color, tooltip) for a non-executed live_exec_status,
    or None if there's nothing worth flagging (empty, or already executed/
    virtual-by-design). Mirrors the reasons written by _try_live_execute /
    _try_re_limit_order in reversal_engine_live_execute.py."""
    if not exec_st or exec_st in ("executed",) or exec_st.startswith("limit_order_placed"):
        return None
    if exec_st == "skipped:live_disabled":
        return None  # live execution off entirely -- not a per-signal problem
    if exec_st == "ml_skipped":
        return ("ML BLOCKED", "orange", "ML gate blocked live execution: predicted R-multiple < 0")
    if exec_st == "bias_skipped":
        return ("BIAS BLOCKED", "orange", "Fill-time bias re-check disagreed with the signal direction")
    if "circuit breaker" in exec_st.lower():
        return ("CIRCUIT BREAKER", "red", exec_st)
    if exec_st.startswith("limit_order_skip"):
        return ("LIMIT ORDER SKIPPED", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("limit_order_rejected"):
        return ("EA REJECTED", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("limit_order_error"):
        return ("LIMIT ORDER ERROR", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("open_failed"):
        return ("OPEN FAILED", "red", exec_st)
    if exec_st.startswith("error"):
        return ("ERROR", "red", exec_st.split(":", 1)[-1] if ":" in exec_st else exec_st)
    return ("NOT EXECUTED", "grey", exec_st)


def _level_type_badge(ltype: str) -> tuple[str, str]:
    """(badge_text, badge_color)"""
    colors = {
        "asia_low":   ("ASIA LO", "indigo"),
        "asia_high":  ("ASIA HI", "indigo"),
        "swing_high": ("SWING H", "purple"),
        "swing_low":  ("SWING L", "purple"),
        "round_10":   ("ROUND10", "teal"),
        "round_5":    ("ROUND5",  "teal"),
        "congestion": ("CONGST",  "orange"),
    }
    return colors.get(ltype, (ltype[:7].upper(), "grey"))


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    eng = re_engine_module.get_instance()

    # ── Header bar ────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700"
    ):
        ui.label("content_copy").classes("material-icons text-yellow-400 text-xl")
        ui.label("Reversal Engine").classes(
            "text-yellow-400 font-bold text-sm tracking-widest"
        )
        live_exec_lbl = ui.label("• VIRTUAL — NO MT5 ORDERS •").classes(
            "text-gray-500 text-xs font-mono"
        )
        with ui.row().classes("ml-auto items-center gap-3"):
            status_badge = ui.badge("—", color="grey").classes("text-xs")
            detail_lbl   = ui.label("").classes("text-xs text-gray-500")
            last_lbl     = ui.label("").classes("text-xs text-gray-600")

    # Controls
    with ui.row().classes("px-4 py-1 gap-2 flex-wrap"):

        async def _start():
            if _is_remote_active():
                await _remote_control("start", "Reversal Engine started (VPS)")
                return
            if eng:
                eng.start()
            ui.notify("Reversal Engine started", type="positive")

        async def _stop():
            if _is_remote_active():
                await _remote_control("stop", "Reversal Engine stopped (VPS)")
                return
            if eng:
                eng.stop()
            ui.notify("Reversal Engine stopped", type="info")

        async def _run_now():
            if _is_remote_active():
                await _remote_control("run_now", "Cycle triggered (VPS)")
                return
            if eng:
                await eng._run_cycle()
            ui.notify("Cycle triggered", type="info")

        async def _remote_control(action: str, success_msg: str) -> None:
            """Send Start/Stop/Run Now to the VPS's own Reversal Engine instead
            of this node's local one, which is stood down in Remote mode and
            would do nothing while the button looked like it worked."""
            cli = sync_client.get_instance()
            if cli is None:
                ui.notify("Not connected to VPS", type="negative")
                return
            try:
                ack = await cli.send_engine_control("reversal_engine", action)
                if ack.get("error"):
                    ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
                else:
                    ui.notify(success_msg, type="positive" if action != "stop" else "info")
            except Exception as e:
                ui.notify(f"Failed to reach VPS: {e}", type="negative")

        ui.button("Start Engine",   icon="play_arrow",   on_click=_start).classes("text-xs")
        ui.button("Stop Engine",    icon="stop",         on_click=_stop).classes("text-xs")
        ui.button("Run Now",        icon="refresh",      on_click=_run_now).classes("text-xs")

    ui.separator()

    # ── Balance banner ────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-6 px-4 py-3 bg-gray-900 border-b border-gray-700"
    ):
        # Realised first, and larger, because it is the one that is real
        # money. The virtual balance beside it is the signal generator's
        # hypothetical ledger over EVERY signal it produced -- including the
        # ~75% that the live-execution gates (ML score, momentum, exposure,
        # schedule, circuit breaker) deliberately blocked and never traded.
        # Those blocked signals are overwhelmingly the losers, so the two
        # numbers diverge hard and in opposite directions: measured
        # 2026-07-31, virtual sat at -$1,651 while the trades actually placed
        # were +$1,076. Showing only the virtual figure made a profitable
        # engine look like it was bleeding.
        with ui.column().classes("items-center"):
            live_pnl_lbl = ui.label("$—").classes("text-2xl font-bold font-mono text-green-400")
            ui.label("Realised P&L (executed)").classes("text-xs text-gray-500")
            live_n_lbl = ui.label("").classes("text-xs text-gray-600")

        ui.separator().props("vertical").classes("h-10 border-gray-700")

        with ui.column().classes("items-center"):
            balance_lbl = ui.label("$—").classes("text-lg font-mono text-gray-400")
            ui.label("Virtual Balance").classes("text-xs text-gray-600")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Hypothetical balance if every signal this engine generated had "
                "been traded, including the ones the live-execution gates blocked. "
                "Useful for judging raw signal quality, not for judging money — "
                "for that, read Realised P&L."
            )

        ui.separator().props("vertical").classes("h-10 border-gray-700")

        with ui.column().classes("items-center"):
            ui.label(f"${_STARTING_BALANCE:,.0f}").classes("text-sm text-gray-400")
            ui.label("Starting").classes("text-xs text-gray-600")

        with ui.column().classes("items-center"):
            pnl_lbl = ui.label("").classes("text-lg font-bold font-mono")
            pnl_pct_lbl = ui.label("Total P&L").classes("text-xs text-gray-500")

        ui.separator().props("vertical").classes("h-10 border-gray-700")

        with ui.column().classes("items-center"):
            dd_lbl = ui.label("$—").classes("text-sm text-orange-400 font-mono")
            ui.label("Max Drawdown").classes("text-xs text-gray-600")

        with ui.column().classes("items-center"):
            wr_lbl = ui.label("—%").classes("text-sm text-blue-300 font-mono")
            wr_sub_lbl = ui.label("Win Rate").classes("text-xs text-gray-600")

    # ── Stats cards ───────────────────────────────────────────────────────────
    stat_cards: dict = {}
    with ui.row().classes("w-full px-4 py-2 gap-3 flex-wrap"):
        for key in ["Total", "Wins", "Losses", "B/E", "Win Rate", "Avg P&L", "Total P&L"]:
            with ui.card().classes("bg-gray-800 px-3 py-2 rounded"):
                ui.label(key).classes("text-xs text-gray-500")
                stat_cards[key] = ui.label("—").classes("text-sm font-bold font-mono text-white")

    ui.separator()

    # ── Main content ──────────────────────────────────────────────────────────
    with ui.row().classes("w-full px-2 py-2 gap-4 items-start flex-wrap"):

        # ── Left column ───────────────────────────────────────────────────────
        with ui.column().classes("flex-1 min-w-72 gap-3"):

            # ── Active candidate levels ───────────────────────────────────────
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label("Active Candidate Levels").classes(
                    "text-sm font-bold text-yellow-300 mb-2"
                )
                levels_container = ui.column().classes("w-full gap-1")

            # ── Open signals ──────────────────────────────────────────────────
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.label("Active Positions").classes(
                        "text-sm font-bold text-yellow-300"
                    )
                    _limit_order_val = bool(db_module.get_risk_settings().get("re_use_limit_order", 0))
                    limit_order_sw = ui.switch(
                        "LIMIT ORDER", value=_limit_order_val,
                    ).classes("text-xs").props("dense color=amber")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "ON: live-executed signals are routed via the EA as a genuine "
                        "MT5 pending limit order at the signal's entry zone. If the EA "
                        "is unreachable when a signal triggers, falls back to the normal "
                        "market-fill flow automatically. OFF: acts as it does now "
                        "(immediate market fill)."
                    )

                    def _toggle_limit_order(e):
                        db_module.update_risk_settings({"re_use_limit_order": 1 if e.value else 0})
                        ui.notify(
                            f"Reversal Engine LIMIT ORDER {'enabled' if e.value else 'disabled'}",
                            type="positive" if e.value else "info",
                        )

                    limit_order_sw.on_value_change(_toggle_limit_order)

                open_container = ui.column().classes("w-full gap-2")

            # ── Signal history ────────────────────────────────────────────────
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label("Signal History").classes(
                    "text-sm font-bold text-yellow-300 mb-2"
                )
                history_container = ui.column().classes("w-full")

        # ── Right column ──────────────────────────────────────────────────────
        with ui.column().classes("gap-3").style("width:380px; min-width:280px"):

            # ── ML Learning status ────────────────────────────────────────────
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label("ML Learning").classes(
                    "text-sm font-bold text-yellow-300 mb-2"
                )
                ml_container = ui.column().classes("w-full gap-1")

            # ── Performance Analytics ─────────────────────────────────────────
            # Same shape as Bounce/Breakout's Performance Analytics card — was
            # missing entirely for Reversal Engine.
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label("Performance Analytics").classes(
                    "text-sm font-bold text-yellow-300 mb-2"
                )
                analytics_container = ui.column().classes("w-full gap-3")

            # ── Cycle log ─────────────────────────────────────────────────────
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label("Cycle Log").classes(
                    "text-sm font-bold text-yellow-300 mb-2"
                )
                log_container = ui.column().classes(
                    "w-full gap-1 overflow-y-auto"
                ).style("max-height:40vh")

    # ── Refresh logic ─────────────────────────────────────────────────────────

    async def _refresh_all():
        # Live execution label (reflects current risk setting, not render-time
        # snapshot) — same bullet-text style as Bounce/Breakout, for a
        # consistent look across all three Signal Generator tabs.
        try:
            from forex_trader.core import database as _main_db
            _rs = await _main_db.to_db_thread(_main_db.get_risk_settings)
            _live = bool(_rs.get("re_live_execution", 0))
        except Exception:
            _live = False
        live_exec_lbl.text = (
            "• LIVE EXECUTION — MT5 ORDERS ACTIVE •" if _live else "• VIRTUAL — NO MT5 ORDERS •"
        )
        live_exec_lbl.classes(
            replace="text-green-400 text-xs font-mono" if _live
            else "text-gray-500 text-xs font-mono"
        )

        # Engine status — in Remote mode this is the VPS's own engine state
        # (mirrored every 3s via the sync heartbeat), NOT this node's local
        # instance, which is stood down and would always show "Stopped"
        # here even while the VPS is actively running it.
        if _is_remote_active():
            cli = sync_client.get_instance()
            is_r = bool((cli.remote_status.get("engines", {}) if cli else {}).get("reversal_engine"))
            status_badge.props(f"color={'green' if is_r else 'grey'}")
            status_badge.text = "Running - Remote" if is_r else "Stopped - Remote"
            detail_lbl.text = ""
            last_lbl.text = ""
        elif eng:
            st = eng.get_status()
            is_r = st["is_running"]
            _suffix = " - Local" if _is_centralized_remote_mode() else ""
            status_badge.props(f"color={'green' if is_r else 'grey'}")
            status_badge.text = f"Running{_suffix}" if is_r else f"Stopped{_suffix}"
            detail_lbl.text   = st["status_msg"]
            lc = st.get("last_cycle_ts")
            last_lbl.text = f"Last cycle: {_fmt_ts(lc)}" if lc else ""

        # Balance
        try:
            bal  = await db_module.to_db_thread(re_db.get_virtual_balance)
            pnl  = bal - _STARTING_BALANCE
            dd   = await db_module.to_db_thread(re_db.get_max_drawdown)
            balance_lbl.text = f"${bal:,.2f}"
            balance_lbl.classes(
                replace="text-lg font-mono "
                + ("text-gray-300" if bal >= _STARTING_BALANCE else "text-red-400")
            )
            pnl_lbl.text = _pnl_str(pnl, "$")
            pnl_lbl.classes(replace=f"text-lg font-bold font-mono {_pnl_color(pnl)}")
            pnl_pct_lbl.text = f"Total P&L ({pnl / _STARTING_BALANCE * 100:+.1f}%)"
            dd_lbl.text = f"${dd:,.2f}"
        except Exception:
            pass

        # Realised P&L -- the trades this engine actually placed, read from
        # the core trade ledger rather than the engine's own virtual one.
        try:
            live = await db_module.to_db_thread(_get_realised_pnl)
            live_pnl_lbl.text = f"${live['total']:,.2f}"
            live_pnl_lbl.classes(
                replace="text-2xl font-bold font-mono "
                + ("text-green-400" if live["total"] >= 0 else "text-red-400")
            )
            live_n_lbl.text = (
                f"{live['n']} closed · {live['per_trade']:+.2f}/trade"
                if live["n"] else "no closed trades yet"
            )
        except Exception:
            pass

        # Stats
        try:
            stats = await db_module.to_db_thread(re_db.get_stats)
            stat_cards["Total"].text    = str(stats["total"])
            stat_cards["Wins"].text     = str(stats["wins"])
            stat_cards["Losses"].text   = str(stats["losses"])
            stat_cards["B/E"].text      = str(stats["bes"])
            stat_cards["Win Rate"].text = f"{stats['win_rate']:.1f}%"
            stat_cards["Avg P&L"].text  = f"${stats['avg_pnl']:+.2f}"
            stat_cards["Total P&L"].text= f"${stats['total_pnl']:+.2f}"
            wr_lbl.text = f"{stats['win_rate']:.1f}%"
            wr_sub_lbl.text = f"Win Rate ({stats['wins']}W / {stats['losses']}L)"
        except Exception:
            pass

        # Active levels from engine cache
        levels_container.clear()
        try:
            cached_levels = eng._cached.get("levels", []) if eng else []
            db_levels     = await db_module.to_db_thread(re_db.get_active_levels)
            display_lvls  = cached_levels[:6] if cached_levels else []

            if display_lvls:
                with levels_container:
                    for lvl in display_lvls:
                        badge_text, badge_color = _level_type_badge(lvl.get("type", ""))
                        with ui.row().classes("w-full items-center gap-2 py-0.5"):
                            ui.badge(badge_text, color=badge_color).classes("text-xs font-mono")
                            ui.label(f"{lvl.get('price', 0):.2f}").classes(
                                f"text-sm font-mono font-bold {_dir_color(lvl.get('direction', 'BUY'))}"
                            )
                            ui.label(f"Score:{lvl.get('score', 0):.2f}").classes("text-xs text-gray-500")
                            ui.label(f"{lvl.get('distance_pts', 0):.1f}pts away").classes("text-xs text-gray-600")
            else:
                with levels_container:
                    ui.label("No candidate levels — engine not running or no price data").classes(
                        "text-xs text-gray-600 italic"
                    )
        except Exception:
            pass

        # Open signals
        open_container.clear()
        try:
            open_sigs = await db_module.to_db_thread(re_db.get_open_signals)
            if open_sigs:
                with open_container:
                    for sig in open_sigs:
                        direction = sig.get("direction", "")
                        border    = "border-green-700" if direction == "BUY" else "border-red-700"
                        badge_text, badge_color = _level_type_badge(sig.get("level_type", ""))

                        with ui.card().classes(f"w-full bg-gray-900 border-l-2 {border} p-2"):
                            with ui.row().classes("items-center gap-2 mb-1"):
                                ui.badge(direction, color="green" if direction == "BUY" else "red"
                                         ).classes("text-xs")
                                ui.badge(badge_text, color=badge_color).classes("text-xs")
                                ui.label(sig.get("signal_ref", "")).classes(
                                    "text-xs text-gray-600 font-mono"
                                )
                                status = sig.get("status", "")
                                ui.badge(status, color="yellow" if status == "pending" else "blue"
                                         ).classes("text-xs ml-auto")
                                _exec_badge = _live_exec_badge(sig.get("live_exec_status") or "")
                                if _exec_badge:
                                    _bt, _bc, _tip = _exec_badge
                                    ui.badge(_bt, color=_bc).classes("text-xs").tooltip(_tip)

                            with ui.row().classes("text-xs text-gray-400 gap-4 flex-wrap"):
                                ui.label(f"Entry: {sig.get('entry_low', 0):.2f}–{sig.get('entry_high', 0):.2f}")
                                ui.label(f"SL: {sig.get('stop_loss', 0):.2f}")
                                ui.label(f"TP1: {sig.get('tp1', 0):.2f}")
                                ui.label(f"TP7: {sig.get('tp7') or sig.get('tp6', '—')}")
                                ui.label(f"Level: {sig.get('level_price', 0):.2f}")
                                ui.label(f"Score: {sig.get('level_score', 0):.2f}")
            else:
                with open_container:
                    ui.label("No open signals").classes("text-xs text-gray-600 italic")
        except Exception:
            pass

        # Signal history — same column set/shape as Bounce (test_panel.py) and
        # Breakout (breakout_panel.py)'s history tables, adapted to Reversal Engine's
        # own fields (level_type/level_price instead of pattern/broken_level,
        # plus the REF correlation lead/lag column those two don't have).
        history_container.clear()
        try:
            all_sigs = await db_module.to_db_thread(re_db.get_all_signals, limit=80)
            closed   = [s for s in all_sigs if s.get("status") == "closed"][:60]

            if closed:
                with history_container:
                    with ui.element("table").classes("w-full text-xs"):
                        with ui.element("thead"):
                            with ui.element("tr").classes("text-gray-500 border-b border-gray-700"):
                                _HDR_TIPS = {
                                    "Live Trade": "MT5 ticket if a real trade was opened. VIRTUAL = learning only.",
                                    "Level Type": "The reference-style price level this signal was based on: round_5/round_10, asia_high/low, swing_high/low",
                                    "Realized R": "Actual outcome relative to the risk this signal took (pnl_pts / sl_dist), not the fixed TP1-vs-SL plan ratio",
                                    "Session":    "Market session when the signal fired",
                                    "Bias":       "H1 higher-timeframe trend bias at signal time",
                                    "Outcome":    "WIN = closed in profit, LOSS = hit SL, BE = break-even",
                                    "Held":       "Time from trigger to close",
                                }
                                for hdr in [
                                    "Ref", "Live Trade", "Opened", "Dir", "Level Type", "Level",
                                    "Entry", "SL", "TP1", "TP2", "Realized R", "Session", "Bias",
                                    "Strategy", "Outcome", "Held", "PnL pts", "PnL $",
                                ]:
                                    with ui.element("th").classes("text-left px-2 py-1 font-medium"):
                                        tip = _HDR_TIPS.get(hdr)
                                        if tip:
                                            ui.label(hdr).classes("cursor-help underline decoration-dotted decoration-gray-600").tooltip(tip)
                                        else:
                                            ui.label(hdr)

                        with ui.element("tbody"):
                            for sig in closed:
                                direction = sig.get("direction", "?")
                                outcome   = sig.get("outcome") or "?"
                                pnl_pts   = sig.get("pnl_pts")
                                pnl_dol   = sig.get("net_pnl_dollars")
                                # Realized R -- actual outcome relative to the risk this
                                # signal actually took (sl_dist), not the static TP1-vs-SL
                                # plan ratio (rr_tp1). Same fix as breakout_panel.py's
                                # Signal History table -- this list is already filtered to
                                # status == 'closed', so realized R is always available.
                                sl_dist_v   = sig.get("sl_dist")
                                realized_rr = (
                                    float(pnl_pts) / float(sl_dist_v)
                                    if pnl_pts is not None and sl_dist_v else None
                                )
                                t_trig    = float(sig.get("trigger_time") or 0)
                                t_close   = float(sig.get("close_time") or 0)
                                held_secs = (t_close - t_trig) if (t_trig and t_close) else 0
                                held_str  = _fmt_duration(held_secs)
                                mid       = ((sig.get("entry_low", 0) or 0) + (sig.get("entry_high", 0) or 0)) / 2
                                badge_t, _ = _level_type_badge(sig.get("level_type", ""))
                                mt5_tkt   = sig.get("mt5_ticket")
                                exec_st   = sig.get("live_exec_status") or ""
                                live_reason = ""
                                if ":" in exec_st:
                                    live_reason = exec_st.split(":", 1)[1].strip()
                                if mt5_tkt:
                                    live_cell = f"MT5 #{mt5_tkt}"
                                    live_cls  = "text-green-400 font-mono font-bold"
                                elif exec_st.startswith("failed") and "circuit breaker" in exec_st.lower():
                                    live_cell = "CIRCUIT BREAKER"
                                    live_cls  = "text-orange-400 font-mono"
                                elif exec_st.startswith("failed"):
                                    live_cell = f"LIVE FAIL: {live_reason[:40]}" if live_reason else "LIVE FAIL"
                                    live_cls  = "text-red-400 font-mono"
                                else:
                                    live_cell = "VIRTUAL"
                                    live_cls  = "text-gray-600 font-mono"

                                with ui.element("tr").classes("border-b border-gray-800 hover:bg-gray-800"):
                                    cells = [
                                        (sig.get("signal_ref", "—")[-8:],                       "text-gray-500 font-mono"),
                                        (live_cell,                                              live_cls),
                                        (_fmt_ts(sig.get("created_at")),                         "text-gray-400"),
                                        (direction,                                               f"{_dir_color(direction)} font-bold"),
                                        (badge_t,                                                 "text-gray-400 text-xs"),
                                        (f"${float(sig.get('level_price') or 0):.2f}",            "text-orange-200"),
                                        (f"${mid:.2f}",                                           "text-gray-200"),
                                        (f"${float(sig.get('stop_loss') or 0):.2f}",              "text-red-300"),
                                        (f"${float(sig.get('tp1') or 0):.2f}" if sig.get("tp1") else "—", "text-green-300"),
                                        (f"${float(sig.get('tp2') or 0):.2f}" if sig.get("tp2") else "—", "text-green-400"),
                                        (f"{realized_rr:+.2f}R" if realized_rr is not None else "—", "text-blue-300"),
                                        (sig.get("session") or "—",                               "text-gray-400"),
                                        (sig.get("htf_bias") or "—",                              "text-gray-400"),
                                        ((sig.get("strategy") or "—").replace("_", " "),          "text-indigo-300 font-mono text-xs"),
                                        (outcome.upper(),                                         f"{_outcome_color(outcome)} font-semibold"),
                                        (held_str,                                                "text-cyan-300 font-mono"),
                                        (_pnl_str(pnl_pts),                                       _pnl_color(pnl_pts)),
                                        (_pnl_str(pnl_dol, "$"),                                  _pnl_color(pnl_dol) + " font-semibold"),
                                    ]
                                    for val, cls in cells:
                                        with ui.element("td").classes(f"px-2 py-1 {cls}"):
                                            lbl = ui.label(val)
                                            if val is live_cell and live_reason:
                                                lbl.tooltip(live_reason)
            else:
                with history_container:
                    ui.label("No closed signals yet").classes("text-xs text-gray-600 italic")
        except Exception:
            pass

        # ML status — scorecard chips + "Is it learning?" trend, same shape as
        # Bounce/Breakout's ML Learning panels (ported from breakout_panel.py).
        ml_container.clear()
        try:
            ml_sum = await db_module.to_db_thread(re_ml.summary)
            mets   = await db_module.to_db_thread(re_ml.get_ml_metrics)
            with ml_container:
                with ui.row().classes("w-full gap-2 flex-wrap"):
                    ui.badge(
                        "Trained" if ml_sum["trained"] else "Untrained",
                        color="green" if ml_sum["trained"] else "grey"
                    ).classes("text-xs")
                    ui.badge(f"Labeled: {ml_sum['labeled_count']}", color="blue").classes("text-xs")
                    ui.badge(f"Min: {ml_sum['min_needed']}", color="grey").classes("text-xs")
                    if ml_sum["has_batch"]:
                        ui.badge("Batch model", color="purple").classes("text-xs")
                    if ml_sum["has_online"]:
                        ui.badge("Online SGD", color="teal").classes("text-xs")

                remaining = max(0, ml_sum["min_needed"] - ml_sum["labeled_count"])
                if remaining > 0:
                    ui.label(f"Needs {remaining} more closed signals before first training").classes(
                        "text-xs text-yellow-500 italic"
                    )

                # ── Scorecard chips ────────────────────────────────────────────
                with ui.row().classes("flex-wrap gap-2 mt-2"):
                    def _chip(label: str, value: str, color: str, tip: str):
                        with ui.card().classes("bg-gray-900 rounded px-2 py-1 text-center min-w-16"):
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
                          "Mean predicted R-multiple (ml_prob) across all closed signals.")

                    act_r_val = mets.get("mean_actual_r")
                    act_r_str = f"{act_r_val:+.3f}" if act_r_val is not None else "—"
                    act_r_col = (
                        "text-green-400"  if act_r_val is not None and act_r_val > 0.0 else
                        "text-yellow-400" if act_r_val is not None and act_r_val > -0.3 else
                        "text-red-400"
                    ) if act_r_val is not None else "text-gray-500"
                    _chip("Act R", act_r_str, act_r_col,
                          "Mean actual R-multiple across closed signals (+R=win, -1=loss, 0=BE). "
                          "Target >0 = edge is positive.")

                    _chip("Labeled", str(mets.get("n_data", 0)), "text-blue-300",
                          "Closed signals with ML probability stored.")

                    needed  = re_ml.MIN_TRAIN_SAMPLES
                    have    = ml_sum.get("labeled_count", 0)
                    next_in = max(0, needed - have) if not ml_sum.get("trained") else \
                              re_ml.RETRAIN_EVERY - (have % re_ml.RETRAIN_EVERY or re_ml.RETRAIN_EVERY)
                    next_str = f"+{next_in}" if ml_sum.get("trained") else f"{have}/{needed}"
                    _chip("Next Train", next_str, "text-cyan-300",
                          f"Retrains every {re_ml.RETRAIN_EVERY} new labeled examples "
                          f"once {re_ml.MIN_TRAIN_SAMPLES} minimum reached.")

                # ── Is it learning? ────────────────────────────────────────────
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

                    def _to_svg_points(series: list, lo: float, hi: float, w: int, h: int) -> str:
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
                                        (str(sig_ids[i])[-14:], "text-gray-500 font-mono"),
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
                        f"No calibration data yet. Need {re_ml.MIN_TRAIN_SAMPLES} "
                        f"closed signals with ML probability stored."
                    ).classes("text-gray-600 text-xs italic mt-1")

                # Feature list
                ui.label("Features:").classes("text-xs text-gray-500 mt-2")
                feat_txt = ", ".join(ml_sum.get("features", []))
                ui.label(feat_txt).classes("text-xs text-gray-600 leading-relaxed")
        except Exception:
            pass

        # Performance Analytics — By Session / By Bias / By Level Type, same
        # shape as Bounce/Breakout's Performance Analytics card.
        analytics_container.clear()
        try:
            by_session = await db_module.to_db_thread(re_db.get_perf_by_session)
            by_bias    = await db_module.to_db_thread(re_db.get_perf_by_bias)
            by_level   = await db_module.to_db_thread(re_db.get_perf_by_level_type)
            with analytics_container:
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

                _perf_table("By Session",    by_session, "session")
                _perf_table("By HTF Bias",   by_bias,    "htf_bias")
                _perf_table("By Level Type", by_level,   "level_type")
                if not (by_session or by_bias or by_level):
                    ui.label("No closed signals yet").classes("text-xs text-gray-600 italic")
        except Exception:
            pass

        # Cycle log
        log_container.clear()
        try:
            log_entries = await db_module.to_db_thread(re_db.get_analysis_log, limit=30)
            with log_container:
                for entry in log_entries:
                    is_signal = entry.get("result") == "signal"
                    bg = "bg-gray-700" if is_signal else "bg-gray-900"
                    with ui.card().classes(f"w-full {bg} p-2 rounded mb-1"):
                        with ui.row().classes("items-center gap-2 mb-0.5"):
                            ui.label(_fmt_ts(entry.get("ts"))).classes(
                                "text-xs font-mono text-gray-500"
                            )
                            ui.label(f"{entry.get('session', '?')}|{entry.get('htf_bias', '?')}").classes(
                                "text-xs text-gray-500"
                            )
                            if is_signal:
                                ui.badge("SIGNAL", color="green").classes("text-xs")
                            res = entry.get("result", "")
                            if res and res != "signal":
                                ui.label(res).classes("text-xs text-gray-600 italic")

                        lvls = entry.get("levels", [])
                        if lvls:
                            lvl_txt = "  ".join(
                                f"{l.get('t', '?')}@{l.get('p', 0):.0f}({l.get('s', 0):.2f})"
                                for l in lvls[:3]
                            )
                            ui.label(lvl_txt).classes("text-xs text-gray-500 font-mono")

                        reason = entry.get("reason")
                        if reason:
                            ui.label(reason).classes("text-xs text-gray-600 italic")
        except Exception:
            pass

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

    # Register refresh callback with engine
    if eng:
        eng.add_refresh_callback(_safe_refresh)

    asyncio.create_task(_refresh_all())

    ui.timer(30, _safe_refresh)
