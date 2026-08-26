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
from typing import Optional

from backend.src.controllers import engines_controller as engines_controller

import logging

from nicegui import ui

_log = logging.getLogger(__name__)

from backend.src.controllers import sync_controller as sync_ctl

from ._sections import _render_history_section, _render_ml_section
from ._shared import _dir_color, _fmt_ts, _level_type_badge, _pnl_color, _pnl_str

_STARTING_BALANCE = 1000.0




# ── Formatting helpers ────────────────────────────────────────────────────────

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


# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    eng = engines_controller.get_engine("reversal")

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
            if sync_ctl.is_remote_active():
                await _remote_control("start", "Reversal Engine started (VPS)")
                return
            if eng:
                eng.start()
            ui.notify("Reversal Engine started", type="positive")

        async def _stop():
            if sync_ctl.is_remote_active():
                await _remote_control("stop", "Reversal Engine stopped (VPS)")
                return
            if eng:
                eng.stop()
            ui.notify("Reversal Engine stopped", type="info")

        async def _run_now():
            if sync_ctl.is_remote_active():
                await _remote_control("run_now", "Cycle triggered (VPS)")
                return
            if eng:
                await eng._run_cycle()
            ui.notify("Cycle triggered", type="info")

        async def _remote_control(action: str, success_msg: str) -> None:
            """Send Start/Stop/Run Now to the VPS's own Reversal Engine instead
            of this node's local one, which is stood down in Remote mode and
            would do nothing while the button looked like it worked."""
            try:
                ack = await sync_ctl.send_engine_control("reversal_engine", action)
                if ack.get("error"):
                    ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
                else:
                    ui.notify(success_msg, type="positive" if action != "stop" else "info")
            except Exception as e:
                ui.notify(f"Failed to reach VPS: {e}", type="negative")

        ui.button("Start Engine",   icon="play_arrow",   on_click=_start).classes("text-xs")
        ui.button("Stop Engine",    icon="stop",         on_click=_stop).classes("text-xs")
        ui.button("Run Now",        icon="refresh",      on_click=_run_now).classes("text-xs")

    # ── Learn From Pro Signals ────────────────────────────────────────────────
    # The toggle described in reversal_engine/pro_model.py: every captured
    # Gold Diggers signal refits that classifier, and its verdict enters this
    # engine's feature vector as `pro_likeness`. Deliberately sits with the
    # engine controls rather than in Settings -- it changes what this engine
    # learns from, so it belongs where its effect is visible.
    with ui.row().classes("px-4 py-1 gap-3 items-center flex-wrap"):
        def _learn_on() -> bool:
            try:
                return bool(engines_controller.get_risk_settings().get("re_learn_from_ref_signals", 0))
            except Exception:
                return False

        learn_status_lbl = ui.label("").classes("text-xs font-mono text-gray-500")

        def _refresh_learn_status() -> None:
            if not _learn_on():
                learn_status_lbl.set_text("off — pro_likeness held at neutral")
                learn_status_lbl.classes(replace="text-xs font-mono text-gray-500")
                return
            try:
                from backend.src.services.reversal_engine import pro_model
                st = pro_model.status()
                c = st.get("corpus") or {}
                base = (f"corpus {c.get('pos', 0)} pro / {c.get('neg', 0)} background · "
                        f"outcomes {c.get('wins', 0)}W-{c.get('losses', 0)}L "
                        f"({c.get('pending', 0)} unresolved)")
                if st.get("ready"):
                    learn_status_lbl.set_text(f"live · AUC {st['auc']:.3f} on n={st['n']} · {base}")
                    learn_status_lbl.classes(replace="text-xs font-mono text-green-400")
                else:
                    learn_status_lbl.set_text(f"collecting — {st.get('reason')} · {base}")
                    learn_status_lbl.classes(replace="text-xs font-mono text-yellow-500")
            except Exception as e:
                learn_status_lbl.set_text(f"unavailable: {e}")
                learn_status_lbl.classes(replace="text-xs font-mono text-gray-500")

        def _toggle_learn(e) -> None:
            engines_controller.update_risk_settings(
                {"re_learn_from_ref_signals": 1 if e.value else 0})
            if e.value:
                try:
                    from backend.src.services.reversal_engine import pro_model
                    pro_model.fit(force=True)
                except Exception as exc:
                    # Toggling the setting still succeeded; only the immediate
                    # refit failed, and it retrains on its own schedule anyway.
                    # Logged rather than swallowed so a persistently broken fit
                    # is visible instead of looking like it worked.
                    _log.warning("[RE-Panel] pro_model refit after enabling "
                                 "Learn From Pro Signals failed: %s", exc)
            ui.notify("Learning from professional signals "
                      f"{'enabled' if e.value else 'disabled'}",
                      type="positive" if e.value else "info")
            _refresh_learn_status()

        ui.switch("Learn From Pro Signals", value=_learn_on(),
                  on_change=_toggle_learn).props("dense").classes("text-xs")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Learns from every Gold Diggers VIP / INSTITUTIONAL signal received: "
            "what the market looked like when they fired, weighted by whether "
            "that call then reached TP1 before its stop. Enters this engine's ML "
            "model as one feature (pro_likeness), never as training rows of its "
            "own. Stays neutral until the corpus is large enough to be honest "
            "and the model beats chance out of sample."
        )
        _refresh_learn_status()
        ui.timer(30.0, _refresh_learn_status)

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
    # Matches breakout_panel.py's stat-tile styling (per-tile color, tooltip,
    # fixed size) for visual consistency across the signal-generator panels.
    stat_cards: dict = {}
    with ui.row().classes("w-full gap-3 flex-wrap px-4 pt-3"):
        for label, color, tip in [
            ("Total Signals", "text-gray-200",  "Total reversal signals generated"),
            ("Wins",          "text-green-400", "Signals that hit TP1 or better"),
            ("Losses",        "text-red-400",   "Signals that hit stop-loss"),
            ("B/E",           "text-yellow-400","Break-even closes (SL moved to entry)"),
            ("Win Rate",      "text-blue-300",  "Wins as % of all closed signals"),
            ("Pending",       "text-gray-400",  "Signals not yet triggered"),
            ("Avg P&L ($)",   "text-gray-200",  "Average dollar P&L per closed trade"),
            ("Total P&L ($)", "text-gray-200",  "Cumulative virtual P&L"),
        ]:
            with ui.card().classes(
                "bg-gray-800 rounded-lg px-3 py-2 text-center flex-none"
                " w-32 min-h-[4.5rem] flex flex-col items-center justify-center"
            ):
                val_lbl = ui.label("—").classes(f"text-lg font-bold {color}")
                ui.label(label).classes("text-xs text-gray-500 leading-tight mt-0.5")
                ui.tooltip(tip)
            stat_cards[label] = val_lbl

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
            _rs = await engines_controller.get_risk_settings_async()
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
        if sync_ctl.is_remote_active():
            _remote = sync_ctl.link_state()["remote_status"]
            is_r = bool(_remote.get("engines", {}).get("reversal_engine"))
            status_badge.props(f"color={'green' if is_r else 'grey'}")
            status_badge.text = "Running - Remote" if is_r else "Stopped - Remote"
            detail_lbl.text = ""
            last_lbl.text = ""
        elif eng:
            st = eng.get_status()
            is_r = st["is_running"]
            _suffix = " - Local" if sync_ctl.is_centralized_remote_mode() else ""
            status_badge.props(f"color={'green' if is_r else 'grey'}")
            status_badge.text = f"Running{_suffix}" if is_r else f"Stopped{_suffix}"
            detail_lbl.text   = st["status_msg"]
            lc = st.get("last_cycle_ts")
            last_lbl.text = f"Last cycle: {_fmt_ts(lc)}" if lc else ""

        # Balance
        try:
            bal  = await engines_controller.reversal.virtual_balance()
            pnl  = bal - _STARTING_BALANCE
            dd   = await engines_controller.reversal.max_drawdown()
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
            live = await engines_controller.reversal_realised_pnl()
            live_pnl_lbl.text = f"${live['total']:,.2f}"
            live_pnl_lbl.classes(
                replace="text-2xl font-bold font-mono "
                + ("text-green-400" if live["total"] >= 0 else "text-red-400")
            )
            live_n_lbl.text = (
                f"{live['n']} closed · {live['per_trade']:+.2f}/trade"
                if live["n"] else "no closed trades yet"
            )
        except Exception as exc:
            # A panel refresh must never take the page down, but a realised-P&L
            # figure that silently stops updating reads as "no trades" -- which
            # is a different claim from "could not read".
            _log.debug("[RE-Panel] realised P&L refresh failed: %s", exc)

        # Stats
        try:
            stats = await engines_controller.reversal.stats()
            stat_cards["Total Signals"].text = str(stats["total"])
            stat_cards["Wins"].text          = str(stats["wins"])
            stat_cards["Losses"].text        = str(stats["losses"])
            stat_cards["B/E"].text           = str(stats["bes"])
            stat_cards["Win Rate"].text      = f"{stats['win_rate']:.1f}%"
            stat_cards["Pending"].text       = str(stats["pending"])
            stat_cards["Avg P&L ($)"].text   = f"${stats['avg_pnl']:+.2f}"
            stat_cards["Avg P&L ($)"].classes(
                replace=f"text-lg font-bold {_pnl_color(stats['avg_pnl'])}"
            )
            stat_cards["Total P&L ($)"].text = f"${stats['total_pnl']:+.2f}"
            stat_cards["Total P&L ($)"].classes(
                replace=f"text-lg font-bold {_pnl_color(stats['total_pnl'])}"
            )
            wr_lbl.text = f"{stats['win_rate']:.1f}%"
            wr_sub_lbl.text = f"Win Rate ({stats['wins']}W / {stats['losses']}L)"
        except Exception:
            pass

        # Active levels from engine cache
        levels_container.clear()
        try:
            cached_levels = eng._cached.get("levels", []) if eng else []
            db_levels     = await engines_controller.reversal.active_levels()
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
            open_sigs = await engines_controller.reversal.open_signals()
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
        await _render_history_section(history_container)

        # ML status — scorecard chips + "Is it learning?" trend, same shape as
        # Bounce/Breakout's ML Learning panels (ported from breakout_panel.py).
        ml_container.clear()
        await _render_ml_section(ml_container)

        # Performance Analytics — By Session / By Bias / By Level Type, same
        # shape as Bounce/Breakout's Performance Analytics card.
        analytics_container.clear()
        try:
            by_session = await engines_controller.reversal.perf_by_session()
            by_bias    = await engines_controller.reversal.perf_by_bias()
            by_level   = await engines_controller.reversal.perf_by_level_type()
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
            log_entries = await engines_controller.reversal.analysis_log(limit=30)
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
