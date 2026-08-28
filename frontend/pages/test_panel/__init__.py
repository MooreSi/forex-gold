"""
TEST panel — password-protected view of the Claude signal generator.

Authentication is tied to this machine's hardware fingerprint via PBKDF2.
The engine runs in the background regardless; auth gates the UI view only.
No MT5 orders, no Telegram messages — test mode only.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from nicegui import ui

from backend.src.controllers import engines_controller as engines_controller

from backend.src.controllers import sync_controller as sync_ctl

from . import _sections
from ._shared import _fmt_ts, _pnl_color, _pnl_str

_STARTING_BALANCE = 1000.0


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
    eng = engines_controller.get_engine("bounce")

    # ── Header bar ────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700"
    ):
        ui.label("science").classes("material-icons text-yellow-400 text-xl")
        ui.label("Bounce Generator").classes(
            "text-yellow-400 font-bold text-sm tracking-widest"
        )
        try:
            _live = bool(engines_controller.get_risk_settings().get("sg_live_execution", 0))
        except Exception:
            _live = False
        ui.label("• LIVE EXECUTION — MT5 ORDERS ACTIVE •" if _live else "• VIRTUAL — NO MT5 ORDERS •").classes(
            "text-green-400 text-xs font-mono" if _live else "text-gray-500 text-xs font-mono"
        )
        with ui.row().classes("ml-auto items-center gap-2"):
            _tg_on = engines_controller.bounce.get_config("tg_learning", "0") == "1"
            tg_chip = ui.badge(
                "TG LEARNING ON" if _tg_on else "TG LEARNING OFF",
                color="teal" if _tg_on else "gray",
            ).classes("text-xs cursor-pointer").tooltip(
                "Feed recent Telegram provider signals to Claude as context"
            )

            def _toggle_tg_learning(chip=tg_chip):
                current = engines_controller.bounce.get_config("tg_learning", "0")
                new_val = "0" if current == "1" else "1"
                engines_controller.bounce.set_config("tg_learning", new_val)
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
        balance    = await engines_controller.bounce.virtual_balance()
        pnl_total  = round(balance - _STARTING_BALANCE, 2)
        pnl_pct    = round((pnl_total / _STARTING_BALANCE) * 100, 1)
        max_dd     = await engines_controller.bounce.max_drawdown()
        stats      = await engines_controller.bounce.stats()
        consec     = await engines_controller.bounce.consecutive_losses()
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
        stats = await engines_controller.bounce.stats()
        balance = await engines_controller.bounce.virtual_balance()
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


            asyncio.create_task(_sections._render_active(active_area=active_area))

            # Closed trades history
            ui.label("Trade History").classes(
                "text-sm font-semibold text-gray-300 uppercase tracking-wider mt-4"
            )
            history_area = ui.column().classes("w-full")


            asyncio.create_task(_sections._render_history(history_area=history_area))

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
                by_session = await engines_controller.bounce.perf_by_session()
                by_bias    = await engines_controller.bounce.perf_by_bias()
                by_level   = await engines_controller.bounce.perf_by_level_type()
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
                all_p     = await engines_controller.bounce.adaptive_params()
                overrides = await engines_controller.bounce.regime_overrides()
                _log_rows = await engines_controller.bounce.analysis_log(limit=60)
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
                                    dflt = engines_controller.bounce.param_specs()[key]["default"]
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

                    # A "Reset to Defaults" button sat here until 2026-08-27.
                    # Removed, not repaired, on the owner's instruction: it had
                    # never worked (NameError on every click), and making it work
                    # would put an unconfirmed wipe of all ~50 learned Bounce
                    # values -- no undo -- on an engine that places real orders.
                    # Full reasoning: docs/todo/bugs/010.

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


            asyncio.create_task(_sections._render_ml(ml_area=ml_area))

            ui.separator().classes("border-gray-700 my-1")

            # Analysis / cycle log
            ui.label("Cycle Log").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            log_area = ui.column().classes("w-full gap-2 overflow-y-auto").style(
                "max-height:40vh"
            )

            async def _render_log():
                entries = await engines_controller.bounce.analysis_log(limit=40)
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
        # Offloaded in one hop -- this runs every 30s tick unconditionally
        # (it IS the diffing check), so three separate reads would be three
        # worker round trips per tick.
        try:
            return await engines_controller.bounce.change_signature()
        except Exception:
            return None

    async def _refresh_all(force: bool = False):
        try:
            sig = await _compute_sig()
            if force or sig is None or sig != _refresh_sig[0]:
                _refresh_sig[0] = sig
                await _render_balance_banner()
                await _render_stats()
                await _sections._render_active(active_area=active_area)
                await _sections._render_history(history_area=history_area)
                await _render_analytics()
                await _render_ap()
                await _render_log()
                await _sections._render_ml(ml_area=ml_area)

            # In Remote mode this is the VPS's own engine state (mirrored
            # every 3s via the sync heartbeat), NOT this node's local
            # instance, which is stood down and would always show "stopped"
            # here even while the VPS is actively running it.
            if sync_ctl.is_remote_active():
                _remote = sync_ctl.link_state()["remote_status"]
                is_r = bool(_remote.get("engines", {}).get("bounce"))
                status_chip.set_text("RUNNING - REMOTE" if is_r else "STOPPED - REMOTE")
                status_chip.props(f"color={'green' if is_r else 'gray'}")
                detail_lbl.set_text("")
            elif eng:
                chip_text = eng.status.upper()
                if sync_ctl.is_centralized_remote_mode():
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
        try:
            ack = await sync_ctl.send_engine_control("bounce", action)
            if ack.get("error"):
                ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
            else:
                ui.notify(success_msg, type="positive" if action != "stop" else "warning")
        except Exception as e:
            ui.notify(f"Failed to reach VPS: {e}", type="negative")

    async def _on_start():
        if sync_ctl.is_remote_active():
            await _remote_control("start", "Test engine started (VPS)")
            return
        if eng:
            engines_controller.bounce.set_config("sg_engine_enabled", "1")
            eng.start()
            await _refresh_all(force=True)
            ui.notify("Test engine started", type="positive")

    async def _on_stop():
        if sync_ctl.is_remote_active():
            await _remote_control("stop", "Test engine stopped (VPS)")
            return
        if eng:
            engines_controller.bounce.set_config("sg_engine_enabled", "0")
            eng.stop()
            await _refresh_all(force=True)
            ui.notify("Test engine stopped", type="warning")

    async def _on_run_now():
        if sync_ctl.is_remote_active():
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
