"""
TEST panel — password-protected view of the Claude signal generator.

Authentication is tied to this machine's hardware fingerprint via PBKDF2.
The engine runs in the background regardless; auth gates the UI view only.
No MT5 orders, no Telegram messages — test mode only.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Callable, Optional

from nicegui import ui

from forex_trader.core import database as db_module

from backend.src.services.test_signal import test_signal_repo as _tdb_real
from backend.src.services.test_signal import test_signal_service as test_engine_module
from backend.src.services.test_signal import adaptive_params as _ap_real
from backend.src.services.test_signal import ml_engine as _ml_real
from forex_trader.sync import client as sync_client
from forex_trader.sync.remote_stats_facade import make_facades, _is_remote_active, _is_centralized_remote_mode

# In Remote mode (VPS is the active trader), these transparently read from
# the mirrored remote signal-gen stats instead of this node's own local
# data — see sync/remote_stats_facade.py. Every other call site below is
# unchanged; the facades expose the same functions as the real modules.
tdb, ml, ap = make_facades("bounce", _tdb_real, _ml_real, _ap_real)

_STARTING_BALANCE = 1000.0


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string: 2h 15m / 45m / 30s."""
    if seconds <= 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    rem = m % 60
    return f"{h}h {rem}m" if rem else f"{h}h"


def _dir_color(direction: str) -> str:
    return "text-green-400" if direction.upper() == "BUY" else "text-red-400"


def _outcome_color(outcome: str) -> str:
    return {
        "win":      "text-green-400",
        "loss":     "text-red-400",
        "be":       "text-yellow-400",
        "tp1_hit":  "text-blue-300",
        "open":     "text-gray-400",
        "pending":  "text-gray-400",
        "expired":  "text-gray-600",
    }.get((outcome or "").lower(), "text-gray-400")


def _status_badge_color(status: str) -> str:
    return {
        "pending":   "bg-yellow-700 text-yellow-100",
        "triggered": "bg-blue-700 text-blue-100",
        "closed":    "bg-gray-700 text-gray-300",
        "expired":   "bg-gray-800 text-gray-500",
    }.get((status or "").lower(), "bg-gray-700 text-gray-300")


def _pnl_color(val: Optional[float]) -> str:
    if val is None:
        return "text-gray-400"
    return "text-green-400" if float(val) >= 0 else "text-red-400"


def _pnl_str(val: Optional[float], prefix: str = "") -> str:
    if val is None:
        return "—"
    return f"{prefix}{float(val):+.2f}"


# ── Main render ───────────────────────────────────────────────────────────────

def render(get_engine: Callable) -> None:
    """Entry point — renders Bounce, Breakout, and Reversal Engine tabs."""
    from frontend.pages import breakout_panel
    from frontend.pages import reversal_panel

    with ui.tabs().classes("bg-gray-900 border-b border-gray-700") as sg_tabs:
        t_bounce   = ui.tab("Bounce",   icon="water")
        t_breakout = ui.tab("Breakout", icon="trending_up")
        t_reversal_engine  = ui.tab("Reversal Engine",  icon="content_copy")

    # animated=False: Quasar's slide transition tracks each panel's position via
    # an internally registered index tied to component identity. NiceGUI re-keys
    # elements on every content rebuild (this page's periodic refresh loops
    # rebuild large chunks of each panel's content), so that index goes stale —
    # confirmed live: switching to Reversal Engine left "aria-selected" correctly true
    # on the Reversal Engine tab while the DOM kept rendering Bounce's content
    # underneath, indefinitely, not just a brief flash. Disabling the animation
    # removes the transform-based positioning calculation that gets this wrong.
    with ui.tab_panels(sg_tabs, value=t_bounce).props("animated=false").classes("bg-gray-900 w-full").style("padding:0"):
        with ui.tab_panel(t_bounce).style("padding:0"):
            _render_main()
        with ui.tab_panel(t_breakout).style("padding:0"):
            breakout_panel.render()
        with ui.tab_panel(t_reversal_engine).style("padding:0"):
            reversal_panel.render()


# ── Bounce (main) panel ───────────────────────────────────────────────────────

def _render_main() -> None:
    eng = test_engine_module.get_instance()

    # ── Header bar ────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700"
    ):
        ui.label("science").classes("material-icons text-yellow-400 text-xl")
        ui.label("Bounce Generator").classes(
            "text-yellow-400 font-bold text-sm tracking-widest"
        )
        try:
            from forex_trader.core import database as _main_db
            _live = bool(_main_db.get_risk_settings().get("sg_live_execution", 0))
        except Exception:
            _live = False
        ui.label("• LIVE EXECUTION — MT5 ORDERS ACTIVE •" if _live else "• VIRTUAL — NO MT5 ORDERS •").classes(
            "text-green-400 text-xs font-mono" if _live else "text-gray-500 text-xs font-mono"
        )
        with ui.row().classes("ml-auto items-center gap-2"):
            _tg_on = tdb.get_config("tg_learning", "0") == "1"
            tg_chip = ui.badge(
                "TG LEARNING ON" if _tg_on else "TG LEARNING OFF",
                color="teal" if _tg_on else "gray",
            ).classes("text-xs cursor-pointer").tooltip(
                "Feed recent Telegram provider signals to Claude as context"
            )

            def _toggle_tg_learning(chip=tg_chip):
                current = tdb.get_config("tg_learning", "0")
                new_val = "0" if current == "1" else "1"
                tdb.set_config("tg_learning", new_val)
                if new_val == "1":
                    chip.text = "TG LEARNING ON"
                    chip.props("color=teal")
                else:
                    chip.text = "TG LEARNING OFF"
                    chip.props("color=gray")

            tg_chip.on("click", _toggle_tg_learning)

            status_chip = ui.badge("stopped", color="gray").classes("text-xs")
            detail_lbl  = ui.label("").classes("text-gray-400 text-xs hidden md:block")

    # ── Balance banner ────────────────────────────────────────────────────────
    balance_row = ui.row().classes(
        "w-full items-center gap-6 px-4 py-3 bg-gray-900 border-b border-gray-700"
    )

    async def _render_balance_banner():
        balance    = await db_module.to_db_thread(tdb.get_virtual_balance)
        pnl_total  = round(balance - _STARTING_BALANCE, 2)
        pnl_pct    = round((pnl_total / _STARTING_BALANCE) * 100, 1)
        max_dd     = await db_module.to_db_thread(tdb.get_max_drawdown)
        stats      = await db_module.to_db_thread(tdb.get_stats)
        consec     = await db_module.to_db_thread(tdb.get_consecutive_losses)
        balance_row.clear()
        with balance_row:
            bal_color  = "text-green-400" if balance >= _STARTING_BALANCE else "text-red-400"
            pnl_color  = "text-green-400" if pnl_total >= 0 else "text-red-400"

            with ui.column().classes("items-center"):
                ui.label(f"${balance:,.2f}").classes(f"text-2xl font-bold {bal_color}")
                ui.label("Virtual Balance").classes("text-xs text-gray-500")

            ui.separator().props("vertical").classes("h-10 border-gray-700")

            with ui.column().classes("items-center"):
                ui.label(f"${_STARTING_BALANCE:,.0f}").classes("text-sm text-gray-400")
                ui.label("Starting").classes("text-xs text-gray-600")

            with ui.column().classes("items-center"):
                ui.label(f"{_pnl_str(pnl_total, '$')}").classes(
                    f"text-lg font-bold {pnl_color}"
                )
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

            if consec >= 2:
                ui.label(f"Warning: {consec} consecutive losses").classes(
                    "ml-auto text-orange-400 text-xs font-semibold"
                )

    asyncio.create_task(_render_balance_banner())

    # ── Controls ──────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 border-b border-gray-700"
    ):
        start_btn   = ui.button("Start Engine",  icon="play_arrow").classes(
            "bg-green-700 hover:bg-green-600 text-white text-xs"
        )
        stop_btn    = ui.button("Stop Engine",   icon="stop").classes(
            "bg-red-800 hover:bg-red-700 text-white text-xs"
        )
        run_now_btn = ui.button("Run Now",       icon="refresh").classes(
            "bg-blue-800 hover:bg-blue-700 text-white text-xs"
        )

    # ── Summary stats row ─────────────────────────────────────────────────────
    stats_row = ui.row().classes("w-full gap-3 flex-wrap px-4 pt-3")

    async def _render_stats():
        stats = await db_module.to_db_thread(tdb.get_stats)
        balance = await db_module.to_db_thread(tdb.get_virtual_balance)
        pnl_total = round(balance - _STARTING_BALANCE, 2)
        stats_row.clear()
        with stats_row:
            for label, value, color, tip in [
                ("Total Signals",  str(stats["total"]),                    "text-gray-200",
                 "Total number of signals generated by the engine (all statuses combined)"),
                ("Wins",           str(stats["wins"]),                     "text-green-400",
                 "Signals that hit TP1 or better and closed in profit"),
                ("Losses",         str(stats["losses"]),                   "text-red-400",
                 "Signals that hit stop-loss and closed at a loss"),
                ("B/E",            str(stats["be"]),                       "text-yellow-400",
                 "Break-even closes: stop-loss moved to entry so the trade closed at zero or near-zero P&L"),
                ("Win Rate",       f"{stats['win_rate']}%",                "text-blue-300",
                 "Wins as a percentage of all closed (non-pending) signals"),
                ("Pending",        str(stats["pending"]),                  "text-gray-400",
                 "Signals waiting for price to reach the entry zone — not yet triggered"),
                ("Avg P&L (pts)",  f"{stats['avg_pnl_pts']:+.1f}",        "text-gray-300",
                 "Average profit/loss per closed trade expressed in price points (1 point = $1 per 0.01 lot)"),
                ("Avg P&L ($)",    f"${stats['avg_pnl_dollars']:+.2f}",   _pnl_color(stats['avg_pnl_dollars']),
                 "Average dollar profit/loss per closed trade based on virtual lot sizing"),
                ("Total P&L ($)",  f"${pnl_total:+.2f}",                  _pnl_color(pnl_total),
                 f"Cumulative virtual P&L since the signal generator started (virtual balance started at ${_STARTING_BALANCE:,.2f})"),
            ]:
                with ui.card().classes(
                    "bg-gray-800 rounded-lg px-3 py-2 text-center flex-none"
                    " w-32 min-h-[4.5rem] flex flex-col items-center justify-center"
                ):
                    ui.label(value).classes(f"text-lg font-bold {color}")
                    ui.label(label).classes("text-xs text-gray-500 leading-tight mt-0.5")
                    ui.tooltip(tip)

    asyncio.create_task(_render_stats())

    # ── Main content — three-column layout ────────────────────────────────────
    with ui.row().classes("w-full flex-1 gap-0"):

        # ── Left: active + closed trades ──────────────────────────────────────
        with ui.column().classes("flex-1 min-w-0 p-4 gap-4"):

            # Active positions
            ui.label("Active Positions").classes(
                "text-sm font-semibold text-yellow-300 uppercase tracking-wider"
            )
            active_area = ui.column().classes("w-full gap-2")

            async def _render_active():
                sigs = await db_module.to_db_thread(tdb.get_open_signals)
                active_area.clear()
                with active_area:
                    if not sigs:
                        ui.label("No active positions").classes("text-gray-600 text-sm italic")
                        return
                    for sig in sigs:
                        direction  = sig.get("direction", "?")
                        entry_mid  = float(sig.get("entry_mid") or 0)
                        sl         = float(sig.get("stop_loss") or 0)
                        tp1        = sig.get("tp1")
                        tp3        = sig.get("tp3")
                        rr         = sig.get("rr_tp1") or 0
                        session    = sig.get("session") or "?"
                        bias       = sig.get("htf_bias") or "?"
                        status     = sig.get("status") or "pending"
                        outcome    = sig.get("outcome") or "open"
                        created    = _fmt_ts(sig.get("created_at"))
                        rationale  = sig.get("rationale") or ""
                        quality    = float(sig.get("quality_score") or 0)
                        kl_type    = sig.get("key_level_type") or "level"
                        lot_size   = float(sig.get("lot_size") or 0.01)
                        risk_amt   = float(sig.get("risk_amount") or 0)
                        sl_moved   = bool(sig.get("sl_moved_to_be"))
                        signal_ref = sig.get("signal_ref") or f"SIG-{sig['id']:04d}"
                        is_fallback = bool(sig.get("claude_fallback"))
                        ml_prob_val = sig.get("ml_prob")
                        _ood_feat   = tdb.get_ml_features_for_signal(sig["id"])
                        _ood_dist   = ml.ood_distance(_ood_feat) if _ood_feat else None

                        border = "border-green-800" if direction == "BUY" else "border-red-800"
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
                                    ui.label(f"{lot_size:.2f}L").classes(
                                        "text-xs text-gray-300 font-mono"
                                    )

                                with ui.column().classes("flex-1 gap-1"):
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label(f"${entry_mid:.2f}").classes(
                                            "text-white font-semibold"
                                        )
                                        sl_label = f"SL ${sl:.2f}" + (" (BE)" if sl_moved else "")
                                        ui.label(sl_label).classes("text-red-300 text-xs")
                                        if tp1:
                                            ui.label(f"TP1 ${float(tp1):.2f}").classes(
                                                "text-green-300 text-xs"
                                            )
                                        if tp3:
                                            ui.label(f"TP3 ${float(tp3):.2f}").classes(
                                                "text-green-400 text-xs"
                                            )
                                        ui.label(f"R:R {rr:.1f}:1").classes(
                                            "text-blue-300 text-xs font-mono"
                                        )

                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label(f"Level: {kl_type}").classes(
                                            "text-gray-400 text-xs"
                                        )
                                        ui.label(f"Session: {session}").classes(
                                            "text-gray-400 text-xs"
                                        )
                                        ui.label(f"Bias: {bias}").classes(
                                            "text-gray-400 text-xs"
                                        )
                                        ui.label(f"Quality: {quality:.0%}").classes(
                                            "text-gray-400 text-xs"
                                        )
                                        ui.label(f"Risk: ${risk_amt:.2f}").classes(
                                            "text-orange-300 text-xs font-mono"
                                        )

                                    if rationale:
                                        ui.label(f'"{rationale}"').classes(
                                            "text-gray-400 text-xs italic mt-1"
                                        )

                                with ui.column().classes("items-end gap-1 shrink-0"):
                                    ui.label(signal_ref).classes(
                                        "text-xs font-mono text-gray-500"
                                    )
                                    ui.label(status.upper()).classes(
                                        f"text-xs font-mono px-2 py-0.5 rounded "
                                        f"{_status_badge_color(status)}"
                                    )
                                    if outcome not in ("open", "pending"):
                                        ui.label(outcome.upper()).classes(
                                            f"text-xs font-bold {_outcome_color(outcome)}"
                                        )
                                    if is_fallback:
                                        ui.label("unreviewed").classes(
                                            "text-xs text-yellow-600 font-mono"
                                        ).tooltip("Approved without Claude review (API unavailable)")
                                    if ml_prob_val is not None:
                                        ui.label(f"ML {ml_prob_val:.0%}").classes(
                                            "text-xs font-mono text-purple-300"
                                        ).tooltip(
                                            f"ML win probability at signal time: {ml_prob_val:.1%}"
                                        )
                                    if _ood_dist is not None:
                                        _ood_color = (
                                            "text-green-400" if _ood_dist < 1.5 else
                                            "text-yellow-400" if _ood_dist < 2.5 else
                                            "text-red-400"
                                        )
                                        _ood_label = (
                                            "familiar" if _ood_dist < 1.5 else
                                            "unusual" if _ood_dist < 2.5 else
                                            "OOD"
                                        )
                                        ui.label(f"OOD {_ood_dist:.1f}").classes(
                                            f"text-xs font-mono {_ood_color}"
                                        ).tooltip(
                                            f"Dissimilarity from training distribution: {_ood_dist:.2f}. "
                                            f"{_ood_label.upper()} — "
                                            "green <1.5 (familiar), amber 1.5-2.5 (unusual), red >2.5 (OOD)"
                                        )
                                    ui.label(created).classes("text-gray-600 text-xs")

            asyncio.create_task(_render_active())

            # Closed trades history
            ui.label("Trade History").classes(
                "text-sm font-semibold text-gray-300 uppercase tracking-wider mt-4"
            )
            history_area = ui.column().classes("w-full")

            async def _render_history():
                sigs   = await db_module.to_db_thread(tdb.get_all_signals, limit=100)
                closed = [s for s in sigs if s.get("status") not in ("pending", "triggered")]
                history_area.clear()
                with history_area:
                    if not closed:
                        ui.label("No completed trades yet").classes(
                            "text-gray-600 text-sm italic"
                        )
                        return

                    with ui.element("table").classes("w-full text-xs"):
                        with ui.element("thead"):
                            with ui.element("tr").classes(
                                "text-gray-500 border-b border-gray-700"
                            ):
                                _HDR_TIPS = {
                                    "Ref":        "Signal reference ID — yellow = AI review unavailable (auto-approved)",
                                    "Live Trade": "MT5 ticket if a real trade was opened. VIRTUAL = learning only. ML SKIP = ML gate blocked. CIRCUIT BREAKER = blocked by consecutive-loss halt. LIVE FAIL = execution error (check logs).",
                                    "Opened":     "Date and time the signal was created",
                                    "Dir":     "Trade direction: BUY (long) or SELL (short)",
                                    "Entry":   "Mid-point of the entry zone price",
                                    "Lot":     "Virtual lot size (0.10 standard = $10/pt at 1:500 leverage)",
                                    "SL":      "Stop-loss price — where the position closes at a loss",
                                    "TP1":     "Take-profit 1 — first target; SL moves to break-even on hit",
                                    "TP3":     "Take-profit 3 — full winner target",
                                    "Realized R": "Actual outcome relative to the risk this signal took (pnl_pts / sl_dist), not the fixed TP1-vs-SL plan ratio",
                                    "Session": "Market session when signal fired: london, overlap, ny, asian",
                                    "Bias":    "H1 Higher Time Frame trend bias at signal time",
                                    "Pattern":   "Entry trigger pattern: bounce, engulfing, pin_bar, rsi_divergence, liquidity_sweep",
                                    "Strategy":  "Active strategy applied when the signal triggered",
                                    "Outcome": "WIN = hit TP3, LOSS = hit SL, BE = break-even SL",
                                    "Held":    "How long the position was open from trigger to close",
                                    "PnL pts": "Profit/loss in price points (1 pt = $10 at 0.10 lots)",
                                    "PnL $":   "Profit/loss in US dollars at the virtual lot size",
                                    "Balance": "Virtual account balance after this trade closed",
                                }
                                for hdr in [
                                    "Ref", "Live Trade", "Opened", "Dir", "Entry", "Lot", "SL",
                                    "TP1", "TP3", "Realized R", "Session", "Bias", "Pattern", "Strategy",
                                    "Outcome", "Held", "PnL pts", "PnL $", "Balance",
                                ]:
                                    with ui.element("th").classes("text-left px-2 py-1 font-medium"):
                                        tip = _HDR_TIPS.get(hdr)
                                        if tip:
                                            ui.label(hdr).classes("cursor-help underline decoration-dotted decoration-gray-600").tooltip(tip)
                                        else:
                                            ui.label(hdr)

                        with ui.element("tbody"):
                            for sig in closed[:60]:
                                direction  = sig.get("direction", "?")
                                outcome    = sig.get("outcome") or "?"
                                pnl_pts    = sig.get("pnl_pts")
                                pnl_dol    = sig.get("pnl_dollars")
                                # Realized R -- actual outcome relative to the risk this
                                # signal actually took (sl_dist), not the static TP1-vs-SL
                                # plan ratio (rr_tp1). Same fix as breakout_panel.py's and
                                # reversal_panel.py's Signal History tables.
                                sl_dist_v   = sig.get("sl_dist")
                                realized_rr = (
                                    float(pnl_pts) / float(sl_dist_v)
                                    if pnl_pts is not None and sl_dist_v else None
                                )
                                bal_after  = sig.get("balance_after")
                                lot_size   = sig.get("lot_size") or 0.01
                                s_ref      = sig.get("signal_ref") or f"SIG-{sig['id']:04d}"
                                s_fallback = bool(sig.get("claude_fallback"))
                                ref_cls    = "text-yellow-600 font-mono" if s_fallback else "text-gray-500 font-mono"
                                t_trigger  = float(sig.get("trigger_time") or 0)
                                t_close    = float(sig.get("close_time") or 0)
                                held_secs  = (t_close - t_trigger) if (t_trigger and t_close) else 0
                                held_str   = _fmt_duration(held_secs)
                                pattern    = (sig.get("trigger_pattern") or "—").replace("_", " ")
                                mt5_tkt    = sig.get("mt5_ticket")
                                exec_st    = sig.get("live_exec_status") or ""
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
                                elif exec_st.startswith("skipped:ml"):
                                    live_cell = "ML SKIP"
                                    live_cls  = "text-yellow-500 font-mono"
                                elif exec_st.startswith("skipped"):
                                    live_cell = "VIRTUAL"
                                    live_cls  = "text-gray-600 font-mono"
                                else:
                                    live_cell = "VIRTUAL"
                                    live_cls  = "text-gray-600 font-mono"
                                with ui.element("tr").classes(
                                    "border-b border-gray-800 hover:bg-gray-800"
                                ):
                                    cells = [
                                        (s_ref, ref_cls),
                                        (live_cell, live_cls),
                                        (_fmt_ts(sig.get("created_at")), "text-gray-400"),
                                        (direction, f"{_dir_color(direction)} font-bold"),
                                        (f"${float(sig.get('entry_mid') or 0):.2f}", "text-gray-200"),
                                        (f"{float(lot_size):.2f}", "text-gray-400 font-mono"),
                                        (f"${float(sig.get('stop_loss') or 0):.2f}", "text-red-300"),
                                        (f"${float(sig.get('tp1') or 0):.2f}" if sig.get("tp1") else "—", "text-green-300"),
                                        (f"${float(sig.get('tp3') or 0):.2f}" if sig.get("tp3") else "—", "text-green-400"),
                                        (f"{realized_rr:+.1f}R" if realized_rr is not None else "—", "text-blue-300"),
                                        (sig.get("session") or "—", "text-gray-400"),
                                        (sig.get("htf_bias") or "—", "text-gray-400"),
                                        (pattern, "text-purple-300 font-mono text-xs"),
                                        ((sig.get("strategy") or "—").replace("_", " "), "text-indigo-300 font-mono text-xs"),
                                        (outcome.upper(), f"{_outcome_color(outcome)} font-semibold"),
                                        (held_str, "text-cyan-300 font-mono"),
                                        (
                                            _pnl_str(pnl_pts),
                                            _pnl_color(pnl_pts),
                                        ),
                                        (
                                            _pnl_str(pnl_dol, "$"),
                                            _pnl_color(pnl_dol) + " font-semibold",
                                        ),
                                        (
                                            f"${float(bal_after):.2f}" if bal_after else "—",
                                            "text-gray-300 font-mono",
                                        ),
                                    ]
                                    for val, cls in cells:
                                        with ui.element("td").classes(f"px-2 py-1 {cls}"):
                                            lbl = ui.label(val)
                                            if val is live_cell and live_reason:
                                                lbl.tooltip(live_reason)

                    # Learning notes for most recent closed trades
                    noted = [s for s in closed[:10] if s.get("learning_note")]
                    if noted:
                        ui.label("Learning Notes").classes(
                            "text-xs font-semibold text-blue-300 uppercase tracking-wider mt-4 mb-1"
                        )
                        for s in noted:
                            outcome = s.get("outcome", "?")
                            with ui.row().classes("items-start gap-2 py-1"):
                                ui.label(
                                    "trending_up" if outcome == "win" else "trending_down"
                                ).classes(
                                    f"material-icons text-xs "
                                    f"{'text-green-400' if outcome == 'win' else 'text-red-400'}"
                                )
                                s_ref = s.get("signal_ref") or f"SIG-{s['id']:04d}"
                                ui.label(
                                    f"{s_ref} {s.get('direction')} {outcome.upper()}: "
                                    f"{s['learning_note']}"
                                ).classes("text-gray-400 text-xs leading-relaxed")

            asyncio.create_task(_render_history())

        # ── Right: analytics + analysis log ──────────────────────────────────
        with ui.column().classes("w-96 shrink-0 border-l border-gray-700 p-4 gap-5"):

            # Performance breakdown
            with ui.row().classes("w-full items-center gap-2"):
                ui.label("Performance Analytics").classes(
                    "text-sm font-semibold text-blue-300 uppercase tracking-wider"
                )
                ui.icon("info").classes("text-gray-500 text-sm cursor-help").tooltip(
                    "Breakdown of virtual signal performance by market session, "
                    "HTF bias direction, and the type of price level the signal was based on. "
                    "Wins = hit TP1+, Losses = hit stop-loss, Avg $ = average dollar P&L per trade, "
                    "Total $ = cumulative dollar P&L for that group."
                )
            analytics_area = ui.column().classes("w-full gap-3")

            async def _render_analytics():
                by_session = await db_module.to_db_thread(tdb.get_perf_by_session)
                by_bias    = await db_module.to_db_thread(tdb.get_perf_by_bias)
                by_level   = await db_module.to_db_thread(tdb.get_perf_by_level_type)
                analytics_area.clear()
                with analytics_area:
                    _COL_TIPS = {
                        "Wins":    "Number of trades in this group that closed at profit (hit TP1 or better)",
                        "Losses":  "Number of trades in this group that hit stop-loss",
                        "Avg $":   "Average dollar profit/loss per trade in this group",
                        "Total $": "Sum of all dollar P&L for trades in this group",
                    }

                    def _perf_table(title: str, rows: list[dict], key_col: str, group_tip: str):
                        if not rows:
                            return
                        with ui.row().classes("items-center gap-1 mt-1"):
                            ui.label(title).classes(
                                "text-xs font-semibold text-gray-400 uppercase tracking-wider"
                            )
                            ui.icon("help_outline").classes(
                                "text-gray-600 text-xs cursor-help"
                            ).tooltip(group_tip)
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-500 border-b border-gray-700"):
                                    col_names = [
                                        (key_col.replace("_", " ").title(), None),
                                        ("Wins",    _COL_TIPS["Wins"]),
                                        ("Losses",  _COL_TIPS["Losses"]),
                                        ("Avg $",   _COL_TIPS["Avg $"]),
                                        ("Total $", _COL_TIPS["Total $"]),
                                    ]
                                    for h, col_tip in col_names:
                                        with ui.element("th").classes("text-left px-1 py-0.5"):
                                            if col_tip:
                                                ui.label(h).classes("cursor-help underline decoration-dotted decoration-gray-600").tooltip(col_tip)
                                            else:
                                                ui.label(h)
                            with ui.element("tbody"):
                                for r in rows:
                                    total_pnl = float(r.get("total_pnl") or 0)
                                    avg_pnl   = float(r.get("avg_pnl") or 0)
                                    wins      = int(r.get("wins", 0))
                                    losses    = int(r.get("losses", 0))
                                    closed    = wins + losses
                                    wr        = f"{round(wins/closed*100)}%" if closed else "—"
                                    with ui.element("tr").classes(
                                        "border-b border-gray-800 hover:bg-gray-800"
                                    ):
                                        cells = [
                                            (str(r.get(key_col) or "?"), "text-gray-300",
                                             f"{closed} closed trades in this group ({wins}W / {losses}L, win rate {wr})"),
                                            (str(wins),      "text-green-400",
                                             f"{wins} winning trades in this group"),
                                            (str(losses),    "text-red-400",
                                             f"{losses} losing trades in this group"),
                                            (f"${avg_pnl:+.2f}", _pnl_color(avg_pnl),
                                             f"Average P&L per trade: ${avg_pnl:+.2f}"),
                                            (f"${total_pnl:+.2f}", _pnl_color(total_pnl) + " font-semibold",
                                             f"Total cumulative P&L: ${total_pnl:+.2f}"),
                                        ]
                                        for v, c, cell_tip in cells:
                                            with ui.element("td").classes(f"px-1 py-0.5 {c}"):
                                                ui.label(v).classes("cursor-default").tooltip(cell_tip)

                    _perf_table(
                        "By Session", by_session, "session",
                        "Performance split by market session when the signal was generated. "
                        "Sessions: Asian (23:00-08:00 UTC, low volume), London (08:00-12:00 UTC, high volatility), "
                        "Overlap (12:00-17:00 UTC, highest liquidity — London + New York), NY (12:00-17:00 UTC).",
                    )
                    _perf_table(
                        "By HTF Bias", by_bias, "htf_bias",
                        "Performance split by Higher Time Frame (H1) directional bias at signal time. "
                        "Bullish = H1 trend up (EMA20 > EMA50 + higher highs), Bearish = H1 trend down. "
                        "A signal aligned with HTF bias has higher probability.",
                    )
                    _perf_table(
                        "By Level Type", by_level, "key_level_type",
                        "Performance split by the type of key price level the signal was based on. "
                        "swing = recent swing high/low, round = psychological round number ($X,X00/$X,X50), "
                        "session = session open/high/low. Levels are where price is most likely to react.",
                    )

            asyncio.create_task(_render_analytics())

            ui.separator().classes("border-gray-700 my-1")

            # Adaptive parameters
            ui.label("Learned Parameters").classes(
                "text-sm font-semibold text-purple-300 uppercase tracking-wider"
            )
            ap_area = ui.column().classes("w-full gap-1")

            async def _render_ap():
                all_p     = await db_module.to_db_thread(ap.get_all)
                overrides = await db_module.to_db_thread(ap.get_regime_overrides)
                _log_rows = await db_module.to_db_thread(tdb.get_analysis_log, limit=60)
                param_changes = [e for e in _log_rows if (e.get("result") or "").startswith("param_")]
                ap_area.clear()
                with ap_area:
                    with ui.element("table").classes("w-full text-xs"):
                        with ui.element("thead"):
                            with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                for h in ["Parameter", "Current", "Default"]:
                                    ui.element("th").classes("text-left px-1 py-0.5").text = h
                        with ui.element("tbody"):
                            for key, info in all_p.items():
                                cur = info["value"]
                                dflt = info["default"]
                                changed = abs(cur - dflt) > 1e-5
                                label = key.replace("_", " ").replace("atr mult", "×ATR")
                                cur_str = f"{cur:.2f}" if cur != int(cur) else str(int(cur))
                                dflt_str = f"{dflt:.2f}" if dflt != int(dflt) else str(int(dflt))
                                with ui.element("tr").classes("border-b border-gray-800"):
                                    with ui.element("td").classes("px-1 py-0.5 text-gray-400"):
                                        ui.label(label)
                                    with ui.element("td").classes(
                                        f"px-1 py-0.5 font-mono font-semibold "
                                        f"{'text-purple-300' if changed else 'text-gray-300'}"
                                    ):
                                        ui.label(cur_str + (" *" if changed else ""))
                                    with ui.element("td").classes("px-1 py-0.5 text-gray-600 font-mono"):
                                        ui.label(dflt_str)

                    ui.label("* = learned value (differs from default)").classes(
                        "text-gray-600 text-xs italic mt-1"
                    )

                    # Regime-specific overrides — these never show as "changed" in
                    # the table above since get_all() only reports each param's
                    # global fallback value, but this is where almost all real
                    # learning actually lands (Claude's batch analysis tags most
                    # adjustments with a regime). Without this section the params
                    # here look permanently stuck at default even when they aren't.
                    if overrides:
                        ui.label("Regime Overrides").classes(
                            "text-xs font-semibold text-purple-300 uppercase tracking-wider mt-2"
                        ).tooltip(
                            "Learned values that apply only when a signal fires under this "
                            "specific market regime — not shown in the Current column above, "
                            "which is the global fallback used when no regime override exists."
                        )
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                    for h in ["Parameter", "Regime", "Value", "Global default"]:
                                        ui.element("th").classes("text-left px-1 py-0.5").text = h
                            with ui.element("tbody"):
                                for key in sorted(overrides.keys()):
                                    dflt = ap.PARAMS[key]["default"]
                                    label = key.replace("_", " ")
                                    for regime, val in sorted(overrides[key].items()):
                                        val_str = f"{val:.2f}" if val != int(val) else str(int(val))
                                        dflt_str = f"{dflt:.2f}" if dflt != int(dflt) else str(int(dflt))
                                        with ui.element("tr").classes("border-b border-gray-800"):
                                            with ui.element("td").classes("px-1 py-0.5 text-gray-400"):
                                                ui.label(label)
                                            with ui.element("td").classes("px-1 py-0.5 text-gray-500 font-mono"):
                                                ui.label(regime)
                                            with ui.element("td").classes("px-1 py-0.5 font-mono font-semibold text-purple-300"):
                                                ui.label(val_str)
                                            with ui.element("td").classes("px-1 py-0.5 text-gray-600 font-mono"):
                                                ui.label(dflt_str)

                    with ui.row().classes("gap-2 mt-2"):
                        def _reset_params():
                            ap.reset_to_defaults()
                            asyncio.create_task(_render_ap())
                            ui.notify("Parameters reset to defaults", type="info")

                        ui.button("Reset to Defaults", on_click=_reset_params).classes(
                            "bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs px-2 py-1"
                        )

                    # Recent parameter change log
                    if param_changes:
                        ui.label("Recent changes").classes(
                            "text-xs text-gray-500 uppercase tracking-wider mt-2"
                        )
                        for c in param_changes[:8]:
                            ui.label(
                                f"{_fmt_ts(c.get('ts'))}  {c.get('claude_decision','')}"
                            ).classes("text-xs text-purple-300 leading-tight py-0.5")

            asyncio.create_task(_render_ap())

            ui.separator().classes("border-gray-700 my-1")

            # ── ML Learning Panel ─────────────────────────────────────────────
            ui.label("ML Learning").classes(
                "text-sm font-semibold text-purple-300 uppercase tracking-wider"
            )
            ml_area = ui.column().classes("w-full gap-2")

            async def _render_ml():
                s    = await db_module.to_db_thread(ml.summary)
                mets = await db_module.to_db_thread(ml.get_ml_metrics)
                ml_area.clear()
                with ml_area:

                    # ── Scorecard chips ───────────────────────────────────────
                    with ui.row().classes("flex-wrap gap-2"):
                        def _chip(label: str, value: str, color: str, tip: str):
                            with ui.card().classes(
                                f"bg-gray-800 rounded px-2 py-1 text-center min-w-16"
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

                        n_data = mets.get("n_data", 0)
                        _chip("Labeled", str(n_data), "text-blue-300",
                              "Number of closed signals with ML probability stored "
                              "(available for calibration analysis).")

                        _chip("Samples", str(s["labeled_samples"]), "text-gray-300",
                              "Total labeled training examples (closed signals with features).")

                        _have    = s["labeled_samples"]
                        _needed  = ml.MIN_TRAIN_SAMPLES
                        _next_in = s["next_train_in"]
                        _next_str = f"+{_next_in}" if s["trained"] else f"{_have}/{_needed}"
                        _chip("Next Train", _next_str, "text-cyan-300",
                              f"Retrains every {ml.RETRAIN_EVERY} new labeled examples "
                              f"once {ml.MIN_TRAIN_SAMPLES} minimum reached. "
                              f"{'Model is trained.' if s['trained'] else 'Not yet trained.'}")

                        _backend = "LGB" if s["backend"].startswith("Light") else "RF"
                        _chip("Backend", _backend, "text-orange-300",
                              f"ML backend: {s['backend']}. "
                              f"Regime models: trending={'yes' if s['regime_trending'] else 'no'}, "
                              f"ranging={'yes' if s['regime_ranging'] else 'no'}. "
                              f"Online learner: {'active' if s.get('online_active') else 'waiting'}.")

                    # ── Is it learning? (cumulative Brier + win rate trend) ───
                    sig_ids      = mets.get("signal_ids", [])
                    win_rates    = mets.get("win_rate_series", [])
                    pred_r_ser   = mets.get("pred_r_series", [])
                    actual_r_ser = mets.get("actual_r_series", [])

                    if sig_ids:
                        ui.label("Is it learning?").classes(
                            "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1"
                        )
                        # SVG sparkline: win rate (green) and cumulative actual R (orange)
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
                        # Cumulative actual R normalised to [-1, 1]
                        cum_r: list = []
                        running = 0.0
                        for v in actual_r_ser:
                            running += v
                            cum_r.append(round(running / len(cum_r + [0]), 3))
                        ar_pts = _to_svg_points(cum_r, -1.0, 1.0, W, H)

                        svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
                                   xmlns="http://www.w3.org/2000/svg"
                                   style="background:#1f2937;border-radius:4px">
                          <!-- zero R reference line -->
                          <line x1="0" y1="{H//2}" x2="{W}" y2="{H//2}"
                                stroke="#374151" stroke-width="1" stroke-dasharray="4,4"/>
                          <!-- Win rate (green) -->
                          {f'<polyline points="{wr_pts}" fill="none" stroke="#4ade80" stroke-width="1.5"/>' if wr_pts else ''}
                          <!-- Cumulative actual R (orange) -->
                          {f'<polyline points="{ar_pts}" fill="none" stroke="#fb923c" stroke-width="1.5" stroke-dasharray="3,2"/>' if ar_pts else ''}
                        </svg>"""
                        ui.html(svg).tooltip(
                            "Green = cumulative win rate (target >50%). "
                            "Orange dashed = mean actual R-multiple (target >0)."
                        )
                        with ui.row().classes("gap-3 text-xs"):
                            ui.label("— win rate").classes("text-green-400")
                            ui.label("--- actual R").classes("text-orange-400")

                        # Last 5 values table
                        last_n = min(5, n)
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                    for h_label in ["Signal", "Win%", "Pred R", "Act R"]:
                                        ui.element("th").classes("text-left px-1 py-0.5").text = h_label
                            with ui.element("tbody"):
                                for i in range(n - last_n, n):
                                    _sid     = sig_ids[i]
                                    _sid_str = f"SIG-{_sid:04d}" if isinstance(_sid, int) else str(_sid)[:14]
                                    _pr      = pred_r_ser[i] if i < len(pred_r_ser) else None
                                    _ar      = actual_r_ser[i] if i < len(actual_r_ser) else None
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        cells = [
                                            (_sid_str, "text-gray-500 font-mono"),
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
                            f"Need {ml.MIN_TRAIN_SAMPLES} closed signals with ML probability stored."
                        ).classes("text-gray-600 text-xs italic")

                    # ── Calibration diagram ───────────────────────────────────
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
                                        ("Drift",     "Deviation from perfect calibration (actual - predicted)"),
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
                                        drift_str = "—"
                                        drift_col = "text-gray-600"
                                        actual_str = "—"
                                        actual_col = "text-gray-600"
                                    else:
                                        drift = actual - pred
                                        drift_str = f"{drift:+.0f}%"
                                        drift_col = (
                                            "text-green-400" if abs(drift) < 10 else
                                            "text-yellow-400" if abs(drift) < 20 else
                                            "text-red-400"
                                        )
                                        actual_str = f"{actual:.0f}%"
                                        actual_col = "text-blue-300"
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        for val, cls in [
                                            (c["label"],     "text-gray-400"),
                                            (f"{pred:.0f}%", "text-purple-300 font-mono"),
                                            (actual_str,     actual_col + " font-mono"),
                                            (str(c["count"]), "text-gray-500"),
                                            (drift_str,      drift_col + " font-mono"),
                                        ]:
                                            with ui.element("td").classes(f"px-1 py-0.5 {val and cls}"):
                                                ui.label(val)

                    # ── Regime performance ────────────────────────────────────
                    regime_rows = tdb.get_perf_by_regime()
                    if regime_rows:
                        ui.label("By Regime").classes(
                            "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-2"
                        ).tooltip(
                            "Performance split by market regime. "
                            "Trending = ADX norm >= 0.5 (directional market). "
                            "Ranging = ADX norm < 0.5 (sideways / consolidating)."
                        )
                        with ui.element("table").classes("w-full text-xs mt-1"):
                            with ui.element("thead"):
                                with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                                    for h_lbl in ["Regime", "W", "L", "BE", "WR%", "Total $"]:
                                        ui.element("th").classes("text-left px-1 py-0.5").text = h_lbl
                            with ui.element("tbody"):
                                for r in regime_rows:
                                    total_pnl = float(r.get("total_pnl") or 0)
                                    with ui.element("tr").classes("border-b border-gray-800"):
                                        for val, cls in [
                                            (r.get("regime", "?").title(), "text-gray-300"),
                                            (str(r.get("wins", 0)),        "text-green-400"),
                                            (str(r.get("losses", 0)),      "text-red-400"),
                                            (str(r.get("be", 0)),          "text-yellow-400"),
                                            (f"{r.get('win_rate', 0):.0f}%", "text-blue-300 font-mono"),
                                            (f"${total_pnl:+.2f}",         _pnl_color(total_pnl) + " font-mono"),
                                        ]:
                                            with ui.element("td").classes(f"px-1 py-0.5 {cls}"):
                                                ui.label(val)

                    # ── Feature importance (last retrain) ─────────────────────
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
                                f"({last_retrain['n_samples']} samples, "
                                f"CV-AUC={last_retrain['cv_auc']:.3f}). "
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

            # Analysis / cycle log
            ui.label("Cycle Log").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            log_area = ui.column().classes("w-full gap-2 overflow-y-auto").style(
                "max-height:40vh"
            )

            async def _render_log():
                entries = await db_module.to_db_thread(tdb.get_analysis_log, limit=40)
                log_area.clear()
                with log_area:
                    if not entries:
                        ui.label("No cycles run yet").classes(
                            "text-gray-600 text-xs italic"
                        )
                        return
                    for entry in entries:
                        ts      = _fmt_ts(entry.get("ts"))
                        session = entry.get("session") or "?"
                        bias    = entry.get("htf_bias") or "?"
                        price   = entry.get("price") or 0
                        atr     = entry.get("atr_m15") or 0
                        result  = entry.get("result") or ""
                        reason  = entry.get("suppressed_reason") or ""
                        claude  = entry.get("claude_decision") or ""

                        is_signal = result.startswith("signal_created")
                        is_batch  = result.startswith("batch_analysis")
                        border    = (
                            "border-green-800" if is_signal else
                            "border-blue-800"  if is_batch  else
                            "border-gray-700"
                        )

                        with ui.card().classes(
                            f"w-full rounded p-2 bg-gray-800 border {border}"
                        ):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(ts).classes("text-gray-500 text-xs font-mono")
                                ui.label(f"{session} | {bias}").classes(
                                    "text-gray-400 text-xs"
                                )

                            if price:
                                ui.label(
                                    f"${float(price):.2f}  ATR {float(atr):.2f}"
                                ).classes("text-gray-300 text-xs font-mono")

                            if is_signal:
                                ui.label("SIGNAL GENERATED").classes(
                                    "text-green-400 text-xs font-bold"
                                )
                            elif is_batch:
                                ui.label(
                                    f"BATCH ANALYSIS ({result})"
                                ).classes("text-blue-400 text-xs font-bold")
                            elif reason:
                                ui.label(reason).classes(
                                    "text-gray-500 text-xs italic leading-tight"
                                )

                            if claude:
                                ui.label(f'"{claude}"').classes(
                                    "text-blue-300 text-xs italic leading-tight"
                                )

            asyncio.create_task(_render_log())

    # ── Refresh orchestrator ──────────────────────────────────────────────────

    # Signature of the data the panels render. The 30s timer rebuilds the eight
    # sections only when this changes — idle ticks no longer clear()+rebuild the
    # DOM, which is what caused the visible flicker. The status chip / last-cycle
    # label below always update (they use set_text, so they don't flicker).
    _refresh_sig = [None]

    async def _compute_sig():
        try:
            def _fetch():
                stats   = tdb.get_stats()
                balance = round(tdb.get_virtual_balance(), 2)
                opens   = tuple(
                    (s.get("id"), s.get("status"), s.get("stop_loss"),
                     s.get("sl_moved_to_be"), s.get("ml_prob"))
                    for s in tdb.get_open_signals()
                )
                return (tuple(sorted(stats.items())), balance, opens)
            # Offloaded — this runs every 30s tick unconditionally (it's the
            # diffing check itself), same class of bug as the render fetches.
            return await db_module.to_db_thread(_fetch)
        except Exception:
            return None

    async def _refresh_all(force: bool = False):
        try:
            sig = await _compute_sig()
            if force or sig is None or sig != _refresh_sig[0]:
                _refresh_sig[0] = sig
                await _render_balance_banner()
                await _render_stats()
                await _render_active()
                await _render_history()
                await _render_analytics()
                await _render_ap()
                await _render_log()
                await _render_ml()

            # In Remote mode this is the VPS's own engine state (mirrored
            # every 3s via the sync heartbeat), NOT this node's local
            # instance, which is stood down and would always show "stopped"
            # here even while the VPS is actively running it.
            if _is_remote_active():
                cli = sync_client.get_instance()
                is_r = bool((cli.remote_status.get("engines", {}) if cli else {}).get("bounce"))
                status_chip.set_text("RUNNING - REMOTE" if is_r else "STOPPED - REMOTE")
                status_chip.props(f"color={'green' if is_r else 'gray'}")
                detail_lbl.set_text("")
            elif eng:
                chip_text = eng.status.upper()
                if _is_centralized_remote_mode():
                    chip_text += " - LOCAL"
                chip_color = "green" if eng.is_running else "gray"
                status_chip.set_text(chip_text)
                status_chip.props(f"color={chip_color}")
                last = eng.last_cycle_at
                detail = eng.status_detail or ""
                if last:
                    detail_lbl.set_text(
                        f"Last cycle: {_fmt_ts(last)}  |  {detail}"
                    )
        except Exception:
            pass

    # ── Control handlers ──────────────────────────────────────────────────────

    async def _remote_control(action: str, success_msg: str) -> None:
        """Send Start/Stop/Run Now to the VPS's own bounce (test) engine
        instead of this node's local one, which is stood down in Remote
        mode and would do nothing while the button looked like it worked."""
        cli = sync_client.get_instance()
        if cli is None:
            ui.notify("Not connected to VPS", type="negative")
            return
        try:
            ack = await cli.send_engine_control("bounce", action)
            if ack.get("error"):
                ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
            else:
                ui.notify(success_msg, type="positive" if action != "stop" else "warning")
        except Exception as e:
            ui.notify(f"Failed to reach VPS: {e}", type="negative")

    async def _on_start():
        if _is_remote_active():
            await _remote_control("start", "Test engine started (VPS)")
            return
        if eng:
            tdb.set_config("sg_engine_enabled", "1")
            eng.start()
            await _refresh_all(force=True)
            ui.notify("Test engine started", type="positive")

    async def _on_stop():
        if _is_remote_active():
            await _remote_control("stop", "Test engine stopped (VPS)")
            return
        if eng:
            tdb.set_config("sg_engine_enabled", "0")
            eng.stop()
            await _refresh_all(force=True)
            ui.notify("Test engine stopped", type="warning")

    async def _on_run_now():
        if _is_remote_active():
            await _remote_control("run_now", "Manual cycle triggered (VPS)")
            return
        if eng and eng.is_running:
            await eng._run_cycle()
            await _refresh_all(force=True)
            ui.notify("Manual cycle complete", type="info")
        elif eng:
            ui.notify("Start the engine first", type="negative")

    start_btn.on("click", _on_start)
    stop_btn.on("click", _on_stop)
    run_now_btn.on("click", _on_run_now)

    def _safe_refresh():
        # The engine's own refresh callback is invoked synchronously
        # (fire-and-forget from its perspective), and _refresh_all is now a
        # coroutine function (its DB reads are offloaded to a worker thread)
        # — schedule it as a task rather than calling it directly, which
        # would silently create-and-drop the coroutine without running it.
        asyncio.create_task(_refresh_all())

    if eng:
        eng.add_refresh_callback(_safe_refresh)
        asyncio.create_task(_refresh_all(force=True))

    ui.timer(30, _refresh_all)
