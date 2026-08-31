"""
Breakout signal engine UI panel.
Completely isolated view — shows only breakout signal data, never bounce data.
"""
from __future__ import annotations

import asyncio

from backend.src.controllers import engines_controller as engines_controller

from nicegui import ui

from backend.src.controllers import sync_controller as sync_ctl

from ._sections import _render_history, _render_ml
from ._shared import _bo_type_badge, _dir_color, _fmt_ts, _pnl_color, _pnl_str

import logging

_log = logging.getLogger(__name__)

# Local/Remote switching now lives in the breakout panel_data service:
# in Remote mode these read the VPS's mirrored stats instead of this
# node's own, and the page cannot tell the difference.

_STARTING_BALANCE = 1000.0


# ── Formatting helpers ────────────────────────────────────────────────────────

# ── Main render ───────────────────────────────────────────────────────────────

def render() -> None:
    eng = engines_controller.get_engine("breakout")

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes(
        "w-full items-center gap-3 px-4 py-2 bg-gray-800 border-b border-gray-700"
    ):
        ui.label("trending_up").classes("material-icons text-orange-400 text-xl")
        ui.label("Breakout Engine").classes(
            "text-orange-400 font-bold text-sm tracking-widest"
        )
        _bo_live_hdr = bool(engines_controller.get_risk_settings().get("bo_live_execution", 0))
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
        balance   = await engines_controller.breakout.virtual_balance()
        pnl_total = round(balance - _STARTING_BALANCE, 2)
        pnl_pct   = round(pnl_total / _STARTING_BALANCE * 100, 1)
        max_dd    = await engines_controller.breakout.max_drawdown()
        stats     = await engines_controller.breakout.stats()
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
        stats   = await engines_controller.breakout.stats()
        balance = await engines_controller.breakout.virtual_balance()
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
                sigs = await engines_controller.breakout.open_signals()
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

            asyncio.create_task(_render_history(history_area=history_area))

        # ── Right: analytics + log ────────────────────────────────────────────
        with ui.column().classes("w-96 shrink-0 border-l border-gray-700 p-4 gap-5"):

            # ── Performance analytics ─────────────────────────────────────────
            ui.label("Performance Analytics").classes(
                "text-sm font-semibold text-orange-300 uppercase tracking-wider"
            )
            analytics_area = ui.column().classes("w-full gap-3")

            async def _render_analytics():
                by_type    = await engines_controller.breakout.perf_by_breakout_type()
                by_adx     = await engines_controller.breakout.perf_by_adx_band()
                by_session = await engines_controller.breakout.perf_by_session()
                by_bias    = await engines_controller.breakout.perf_by_bias()
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
                all_p = await engines_controller.breakout.adaptive_params()
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
                        engines_controller.breakout.reset_adaptive_params()
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

            asyncio.create_task(_render_ml(ml_area=ml_area))

            ui.separator().classes("border-gray-700 my-1")

            # ── Cycle log ─────────────────────────────────────────────────────
            ui.label("Cycle Log").classes(
                "text-sm font-semibold text-gray-400 uppercase tracking-wider"
            )
            log_area = ui.column().classes("w-full gap-2 overflow-y-auto").style(
                "max-height:50vh"
            )

            async def _render_log():
                entries = await engines_controller.breakout.analysis_log(limit=40)
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
            await _render_history(history_area=history_area)
            await _render_analytics()
            await _render_ap()
            await _render_ml(ml_area=ml_area)
            await _render_log()

            # Update live/virtual execution label in header
            _rs_live = await engines_controller.get_risk_settings_async()
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
            if sync_ctl.is_remote_active():
                _remote = sync_ctl.link_state()["remote_status"]
                is_r = bool(_remote.get("engines", {}).get("breakout"))
                status_chip.set_text("RUNNING - REMOTE" if is_r else "STOPPED - REMOTE")
                status_chip.props(f"color={'orange' if is_r else 'gray'}")
                detail_lbl.set_text("")
            elif eng:
                chip_text  = eng.status.upper() if eng.is_running else "stopped"
                if sync_ctl.is_centralized_remote_mode():
                    chip_text += " - LOCAL"
                chip_color = "orange" if eng.is_running else "gray"
                status_chip.set_text(chip_text)
                status_chip.props(f"color={chip_color}")
                if eng.last_cycle_at:
                    detail_lbl.set_text(
                        f"Last: {_fmt_ts(eng.last_cycle_at)}  {eng.status_detail or ''}"
                    )
        except Exception as e:
            _log.debug("[breakout panel] status line refresh failed: %s", e)

    # ── Control handlers ──────────────────────────────────────────────────────

    async def _remote_control(action: str, success_msg: str) -> None:
        """Send Start/Stop/Run Now to the VPS's own breakout engine instead
        of this node's local one, which is stood down in Remote mode and
        would do nothing while the button looked like it worked."""
        try:
            ack = await sync_ctl.send_engine_control("breakout", action)
            if ack.get("error"):
                ui.notify(f"VPS rejected request: {ack['error']}", type="negative")
            else:
                ui.notify(success_msg, type="positive" if action != "stop" else "warning")
        except Exception as e:
            ui.notify(f"Failed to reach VPS: {e}", type="negative")

    async def _on_start():
        if sync_ctl.is_remote_active():
            await _remote_control("start", "Breakout engine started (VPS)")
            return
        if eng:
            engines_controller.breakout.set_config("bo_engine_enabled", "1")
            eng.start()
            await _refresh_all()
            ui.notify("Breakout engine started", type="positive")

    async def _on_stop():
        if sync_ctl.is_remote_active():
            await _remote_control("stop", "Breakout engine stopped (VPS)")
            return
        if eng:
            engines_controller.breakout.set_config("bo_engine_enabled", "0")
            eng.stop()
            await _refresh_all()
            ui.notify("Breakout engine stopped", type="warning")

    async def _on_run_now():
        if sync_ctl.is_remote_active():
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
