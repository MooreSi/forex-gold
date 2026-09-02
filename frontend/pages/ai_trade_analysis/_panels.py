"""The four panels the AI Trade Analysis page draws.

Signal generator, channel header, per-channel analysis and strategy-vs-DPM.
All four were already module-level functions taking their data explicitly, so
they move as they are -- checked before the move: each needs nothing from
module scope but `ui` and its own colour/label constants, which come with it.

Here rather than in __init__.py to keep that file to the page's flow: gather,
call the provider, render. Bodies are verbatim.
"""
from nicegui import ui


_TREND_COLOR = {
    "improving":        "text-green-400",
    "stable":           "text-gray-300",
    "declining":        "text-red-400",
    "insufficient_data": "text-gray-500",
}

_TREND_ICON = {
    "improving":        "trending_up",
    "stable":           "trending_flat",
    "declining":        "trending_down",
    "insufficient_data": "help_outline",
}

def _render_signal_generator_panel(data: dict, analysis: dict):
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4 mb-4"):
            ui.label(f"Signal generator analysis error: {analysis['error']}").classes(
                "text-red-300 text-sm"
            )
        return

    with ui.card().classes("w-full bg-gray-900 border border-purple-700 p-4 mb-4"):
        with ui.row().classes("items-center gap-3 mb-3 flex-wrap"):
            ui.icon("auto_awesome", color="purple-400")
            ui.label("Signal Generator Development").classes("text-purple-300 font-bold text-sm")
            ui.badge(f"{data['total_trades']} engine trades  |  {data['days']}d window",
                     color="purple").classes("text-xs")

        overall = analysis.get("overall_assessment", "")
        if overall:
            ui.label(overall).classes("text-gray-300 text-xs leading-relaxed mb-3")

        engines = analysis.get("engines", [])
        if engines:
            cols = min(len(engines), 3)
            with ui.element("div").style(
                f"display:grid; grid-template-columns:repeat({cols},1fr); gap:12px; width:100%;"
            ):
                for eng in engines:
                    trend     = eng.get("trend", "insufficient_data")
                    trend_cls = _TREND_COLOR.get(trend, "text-gray-400")
                    trend_ico = _TREND_ICON.get(trend, "help_outline")
                    is_pro    = eng.get("acting_like_pro_trader", False)
                    pro_cls   = "text-green-400" if is_pro else "text-red-400"
                    pro_lbl   = "Pro-level" if is_pro else "Not yet pro"

                    with ui.card().classes("bg-gray-800 border border-purple-900 rounded-lg p-3"):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon(trend_ico, color=trend_cls.replace("text-", ""), size="xs")
                            ui.label(eng.get("name", "")).classes(
                                "text-white font-semibold text-xs"
                            )
                            ui.space()
                            ui.label(pro_lbl).classes(f"text-xs font-bold {pro_cls}")

                        if eng.get("verdict"):
                            ui.label(eng["verdict"]).classes(
                                "text-xs text-gray-300 leading-relaxed mb-2"
                            )

                        ui.separator().classes("my-1")
                        for lbl, key, icon_name, icon_color in [
                            ("ML contribution",     "ml_contribution",       "memory",     "blue-300"),
                            ("Self-learning",        "self_learning_progress","school",     "teal-300"),
                            ("Key strength",         "key_strength",          "thumb_up",   "green-400"),
                            ("Key weakness",         "key_weakness",          "thumb_down", "orange-400"),
                            ("Recommendation",       "recommendation",        "lightbulb",  "yellow-300"),
                        ]:
                            val = eng.get(key, "")
                            if val:
                                with ui.row().classes("items-start gap-1 mt-1"):
                                    ui.icon(icon_name, color=icon_color, size="xs").classes(
                                        "mt-0.5 shrink-0"
                                    )
                                    with ui.column().classes("gap-0"):
                                        ui.label(lbl).classes("text-xs text-gray-500 font-semibold")
                                        ui.label(val).classes("text-xs text-gray-300 leading-relaxed")

        collective = analysis.get("collective_verdict", "")
        what_needed = analysis.get("what_would_make_them_professional", "")
        if collective or what_needed:
            ui.separator().classes("my-3")
            if collective:
                with ui.row().classes("items-start gap-2"):
                    ui.icon("groups", color="purple-300", size="xs").classes("mt-0.5")
                    ui.label("Collective verdict: ").classes(
                        "text-xs text-purple-300 font-semibold shrink-0"
                    )
                ui.label(collective).classes("text-xs text-gray-300 leading-relaxed mt-1")
            if what_needed:
                with ui.card().classes("bg-purple-900/20 border border-purple-800 rounded-lg p-3 mt-2"):
                    with ui.row().classes("items-center gap-2 mb-1"):
                        ui.icon("emoji_events", color="yellow-300", size="xs")
                        ui.label("What would make them professional?").classes(
                            "text-yellow-300 font-semibold text-xs"
                        )
                    ui.label(what_needed).classes("text-xs text-gray-200 leading-relaxed")

def _render_channel_header(ch: dict):
    stats = ch["stats"]
    with ui.card().classes("w-full bg-gray-900 p-4"):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon("telegram", color="blue-400")
            ui.label(ch["channel_name"]).classes("text-white font-bold text-sm")

        with ui.row().classes("gap-3 flex-wrap"):
            for label, val, cls in [
                (f"{stats['total_signals']} signals", "", "text-gray-300"),
                (f"{stats['closed_trades']} closed", "", "text-gray-300"),
                (f"{float(stats['win_rate_pct'] or 0):.0f}% win rate",
                 "", "text-green-400" if (stats["win_rate_pct"] or 0) >= 50 else "text-red-400"),
                (f"${float(stats['total_pnl'] or 0):+.2f}",
                 "", "text-green-400" if (stats["total_pnl"] or 0) >= 0 else "text-red-400"),
                (f"{stats['phantom_tp_count']} phantom TPs",
                 "", "text-orange-400" if stats["phantom_tp_count"] else "text-gray-500"),
            ]:
                ui.label(label).classes(f"text-xs font-semibold {cls}")

def _render_channel_analysis(ch: dict, analysis: dict):  # noqa: C901
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4"):
            ui.label(f"Analysis error: {analysis['error']}").classes("text-red-300 text-sm")
        return

    stats = ch["stats"]

    # ── Score card ─────────────────────────────────────────────────────────────
    score = int(analysis.get("reliability_score", 0))
    score_cls = "text-green-400" if score >= 75 else "text-yellow-400" if score >= 50 else "text-red-400"
    score_bg  = "bg-green-900/30 border-green-700" if score >= 75 else \
                "bg-yellow-900/30 border-yellow-700" if score >= 50 else \
                "bg-red-900/30 border-red-700"

    with ui.card().classes(f"w-full {score_bg} border p-4"):
        with ui.row().classes("items-center gap-4"):
            ui.label(f"{score}").classes(f"text-4xl font-black {score_cls}")
            with ui.column().classes("gap-0"):
                ui.label("/100 Reliability").classes("text-xs text-gray-400")
                ui.label(analysis.get("reliability_label", "")).classes("text-white text-sm font-semibold")
        ui.separator().classes("my-2")
        ui.label(analysis.get("executive_summary", "")).classes("text-gray-300 text-xs leading-relaxed")

    # ── Pre-flight stats ───────────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-900 p-4"):
        with ui.grid(columns=3).classes("w-full gap-2 text-center"):
            for lbl, val, cls in [
                ("Win Rate",    f"{float(stats['win_rate_pct'] or 0):.1f}%",   "text-white"),
                ("SL Hits",     str(stats["sl_hits"]),              "text-red-400"),
                ("Phantom TPs", str(stats["phantom_tp_count"]),     "text-orange-400" if stats["phantom_tp_count"] else "text-gray-500"),
                ("Max Consec.", f"{stats['max_consecutive_losses']} losses", "text-red-400" if (stats["max_consecutive_losses"] or 0) >= 3 else "text-gray-300"),
                ("Actual P&L",  f"${float(stats['actual_pnl_sum'] or 0):+.2f}", "text-green-400" if (stats["actual_pnl_sum"] or 0) >= 0 else "text-red-400"),
                ("Sim. P&L",    f"${float(stats['simulated_50pct_pnl_sum'] or 0):+.2f}", "text-blue-400"),
            ]:
                with ui.column().classes("items-center gap-0.5 bg-gray-800 rounded p-2"):
                    ui.label(val).classes(f"text-sm font-bold {cls}")
                    ui.label(lbl).classes("text-xs text-gray-500")

    # ── Phantom TP section ─────────────────────────────────────────────────────
    phantoms = analysis.get("phantom_tps", [])
    phantom_pattern = analysis.get("phantom_tp_pattern", "")
    if phantoms or phantom_pattern:
        with ui.card().classes("w-full bg-red-900/20 border border-red-800 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("warning", color="red-400")
                ui.label("Phantom TP Detection").classes("text-red-300 font-semibold text-sm")

            if phantom_pattern:
                ui.label(phantom_pattern).classes("text-gray-300 text-xs mb-2 leading-relaxed")

            for ph in phantoms:
                with ui.row().classes("items-start gap-2 py-1 border-t border-red-900"):
                    with ui.column().classes("gap-0"):
                        ui.label(
                            f"{ph.get('date', '?')} {ph.get('direction', '')}"
                        ).classes("text-xs text-gray-400 font-mono")
                        ui.label(
                            f"Claimed: {ph.get('claimed', '?')}  |  Actual: {ph.get('actual', '?')}  |  "
                            f"P&L: ${float(ph.get('pnl', 0)):+.2f}"
                        ).classes("text-xs text-red-300")
                        if ph.get("detail"):
                            ui.label(ph["detail"]).classes("text-xs text-gray-500 mt-0.5")

    # ── SL management ─────────────────────────────────────────────────────────
    sl_mgmt = analysis.get("sl_management", {})
    if sl_mgmt:
        has_gap = not sl_mgmt.get("channel_instructs_be", True)
        bg = "bg-orange-900/20 border-orange-800" if has_gap else "bg-gray-900"
        with ui.card().classes(f"w-full {bg} border p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("security", color="orange-300")
                ui.label("SL Management").classes("text-orange-300 font-semibold text-sm")
            if sl_mgmt.get("current_weakness"):
                ui.label(f"Issue: {sl_mgmt['current_weakness']}").classes(
                    "text-xs text-gray-300 mb-1"
                )
            if sl_mgmt.get("recommended_rule"):
                with ui.row().classes("items-start gap-2 bg-gray-800 rounded p-2 mt-1"):
                    ui.icon("check_circle", color="green-400", size="xs")
                    ui.label(sl_mgmt["recommended_rule"]).classes(
                        "text-xs text-green-300 font-semibold flex-1"
                    )
            if sl_mgmt.get("implementation_note"):
                ui.label(sl_mgmt["implementation_note"]).classes(
                    "text-xs text-gray-500 mt-1 italic"
                )

    # ── Partial close model ────────────────────────────────────────────────────
    pcm = analysis.get("partial_close_model", {})
    if pcm:
        improvement = float(pcm.get("improvement", 0))
        imp_cls = "text-green-400" if improvement > 0 else "text-red-400"
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("call_split", color="blue-300")
                ui.label("Partial Close Model (50% at TP1)").classes("text-blue-300 font-semibold text-sm")
            with ui.row().classes("gap-4"):
                for lbl, val, cls in [
                    ("Actual P&L",     f"${float(pcm.get('actual_period_pnl', 0)):+.2f}", "text-red-400" if float(pcm.get('actual_period_pnl', 0)) < 0 else "text-green-400"),
                    ("Simulated P&L",  f"${float(pcm.get('simulated_50pct_tp1_pnl', 0)):+.2f}", "text-blue-400"),
                    ("Improvement",    f"${improvement:+.2f}", imp_cls),
                ]:
                    with ui.column().classes("items-center bg-gray-800 rounded px-3 py-2 gap-0"):
                        ui.label(val).classes(f"text-sm font-bold {cls}")
                        ui.label(lbl).classes("text-xs text-gray-500")
            if pcm.get("verdict"):
                ui.label(pcm["verdict"]).classes("text-xs text-gray-400 mt-2 italic leading-relaxed")

    # ── R:R analysis ──────────────────────────────────────────────────────────
    rr = analysis.get("rr_analysis", {})
    if rr and (rr.get("signals_below_1_to_1", 0) > 0 or rr.get("comment")):
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("balance", color="purple-300")
                ui.label("R:R Reality Check").classes("text-purple-300 font-semibold text-sm")
            if rr.get("comment"):
                ui.label(rr["comment"]).classes("text-xs text-gray-300 leading-relaxed")
            for flag in (rr.get("flags") or []):
                with ui.row().classes("items-center gap-1 mt-1"):
                    ui.icon("flag", color="orange-400", size="xs")
                    ui.label(flag).classes("text-xs text-orange-300")

    # ── Session + Entry drift (side-by-side) ───────────────────────────────────
    with ui.row().classes("w-full gap-3"):
        with ui.card().classes("flex-1 bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("schedule", color="cyan-300")
                ui.label("Session Bias").classes("text-cyan-300 font-semibold text-sm")
            ui.label(analysis.get("session_analysis", "—")).classes(
                "text-xs text-gray-300 leading-relaxed"
            )
            if stats["session_pnl"]:
                ui.separator().classes("my-1")
                for sess, pnl in sorted(stats["session_pnl"].items(), key=lambda x: x[1]):
                    col = "text-green-400" if pnl >= 0 else "text-red-400"
                    ui.label(f"{sess}: ${pnl:+.2f}").classes(f"text-xs font-mono {col}")

        drift = analysis.get("entry_drift", {})
        if drift:
            with ui.card().classes("flex-1 bg-gray-900 p-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("swap_horiz", color="teal-300")
                    ui.label("Entry Drift").classes("text-teal-300 font-semibold text-sm")
                with ui.row().classes("gap-3"):
                    for lbl, val in [("Avg pips", f"{drift.get('avg_pips', 0):.1f}"),
                                     ("Worst", f"{drift.get('worst_case_pips', 0):.1f}")]:
                        with ui.column().classes("items-center bg-gray-800 rounded px-3 py-1 gap-0"):
                            ui.label(val).classes("text-sm font-bold text-white")
                            ui.label(lbl).classes("text-xs text-gray-500")
                if drift.get("comment"):
                    ui.label(drift["comment"]).classes("text-xs text-gray-400 mt-2 leading-relaxed")

    # ── Consecutive losses ─────────────────────────────────────────────────────
    consec = analysis.get("consecutive_losses", "")
    if consec:
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("trending_down", color="red-300")
                ui.label("Consecutive Loss Patterns").classes("text-red-300 font-semibold text-sm")
            ui.label(consec).classes("text-xs text-gray-300 leading-relaxed")

    # ── Lot sizing ─────────────────────────────────────────────────────────────
    ls = analysis.get("lot_sizing", {})
    if ls:
        with ui.card().classes("w-full bg-gray-900 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("percent", color="lime-300")
                ui.label("Lot Sizing Recommendation").classes("text-lime-300 font-semibold text-sm")
            with ui.row().classes("gap-3"):
                for lbl, val, cls in [
                    ("Actual Win Rate",   f"{ls.get('actual_win_rate_pct', 0):.1f}%",  "text-white"),
                    ("Kelly Fraction",    f"{ls.get('kelly_fraction_pct', 0):.1f}%",   "text-yellow-300"),
                    ("Recommended Risk",  f"{ls.get('recommended_risk_pct', 0):.2f}%", "text-green-400"),
                ]:
                    with ui.column().classes("items-center bg-gray-800 rounded px-3 py-2 gap-0"):
                        ui.label(val).classes(f"text-sm font-bold {cls}")
                        ui.label(lbl).classes("text-xs text-gray-500")
            if ls.get("reasoning"):
                ui.label(ls["reasoning"]).classes("text-xs text-gray-400 mt-2 leading-relaxed")

    # ── Overall recommendation ─────────────────────────────────────────────────
    rec = analysis.get("overall_recommendation", "")
    if rec:
        with ui.card().classes("w-full bg-blue-900/20 border border-blue-800 p-4"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("lightbulb", color="blue-300")
                ui.label("Overall Recommendation").classes("text-blue-300 font-semibold text-sm")
            ui.label(rec).classes("text-gray-200 text-xs leading-relaxed whitespace-pre-line")

_APPROACH_LABELS = {
    "dpm":              "DPM (Adaptive)",
    "scale_out":        "Scale Out",
    "be_runner":        "BE Runner",
    "trail_stop":       "Trail Stop",
    "protected_scale":  "Protected Scale",
    "insufficient_data": "Insufficient Data",
}

_REC_COLOR = {
    "keep_dpm":           "text-green-400",
    "disable_dpm":        "text-red-400",
    "use_dpm_selectively": "text-yellow-400",
}

def _render_strategy_dpm_panel(data: dict, analysis: dict):  # noqa: C901
    if "error" in analysis:
        with ui.card().classes("w-full bg-red-900/30 border border-red-700 p-4 mb-4"):
            ui.label(f"Strategy/DPM analysis error: {analysis['error']}").classes(
                "text-red-300 text-sm"
            )
        return

    best = analysis.get("best_approach", "insufficient_data")
    best_label = _APPROACH_LABELS.get(best, best)
    dpm_rec    = analysis.get("dpm_assessment", {}).get("recommendation", "")
    rec_cls    = _REC_COLOR.get(dpm_rec, "text-white")

    with ui.card().classes(
        "w-full bg-gray-900 border border-teal-700 p-4 mb-4"
    ):
        # Header
        with ui.row().classes("items-center gap-3 mb-3 flex-wrap"):
            ui.icon("tune", color="teal-400")
            ui.label("Strategy vs DPM Analysis").classes(
                "text-teal-300 font-bold text-sm"
            )
            ui.badge(
                f"Best approach: {best_label}",
                color="teal",
            ).classes("text-xs")
            ui.space()
            ui.label(
                f"{data['total_closed']} closed trades  |  {data['days']}d window"
            ).classes("text-xs text-gray-500")

        # Head-to-head stats grid
        dpm_s   = data["dpm_stats"]
        fixed_s = data["fixed_stats"]

        with ui.element("div").style(
            "display:grid; grid-template-columns:repeat(2,1fr); gap:12px; width:100%;"
        ):
            for label, s, border_cls in [
                ("DPM-Managed Trades",          dpm_s,   "border-teal-700"),
                ("Fixed-Strategy Trades",        fixed_s, "border-gray-600"),
            ]:
                bg_cls = "bg-teal-900/20" if "DPM" in label else "bg-gray-800"
                with ui.card().classes(f"{bg_cls} border {border_cls} rounded-lg p-3"):
                    ui.label(label).classes("text-xs text-gray-400 font-semibold mb-2 uppercase")
                    if s["count"] == 0:
                        ui.label("No trades in period").classes("text-xs text-gray-600 italic")
                    else:
                        with ui.element("div").style(
                            "display:grid; grid-template-columns:repeat(3,1fr); gap:6px;"
                        ):
                            for lbl, val, cls in [
                                ("Trades",    str(s["count"]),                   "text-white"),
                                ("Win Rate",  f"{s['win_rate']}%",               "text-green-400" if s["win_rate"] >= 50 else "text-red-400"),
                                ("Total P&L", f"${s['total_pnl']:+.2f}",         "text-green-400" if s["total_pnl"] >= 0 else "text-red-400"),
                                ("Avg P&L",   f"${s['avg_pnl']:+.2f}",           "text-green-400" if s["avg_pnl"] >= 0 else "text-red-400"),
                                ("Prof. Factor", f"{s['profit_factor']}",        "text-yellow-300"),
                                ("Avg Hold",  f"{s['avg_hold_min']:.0f}m",       "text-gray-300"),
                            ]:
                                with ui.column().classes("items-center gap-0 bg-gray-900 rounded p-1"):
                                    ui.label(val).classes(f"text-xs font-bold {cls}")
                                    ui.label(lbl).classes("text-xs text-gray-600")

        # Per fixed-strategy breakdown
        if data["strategy_breakdown"]:
            ui.separator().classes("my-3")
            ui.label("Fixed Strategy Breakdown").classes(
                "text-xs text-gray-400 uppercase font-semibold mb-2"
            )
            cols = min(len(data["strategy_breakdown"]), 4)
            with ui.element("div").style(
                f"display:grid; grid-template-columns:repeat({cols},1fr); gap:8px; width:100%;"
            ):
                for s in data["strategy_breakdown"]:
                    sl = _APPROACH_LABELS.get(s["strategy"], s["strategy"])
                    pnl_cls = "text-green-400" if s["total_pnl"] >= 0 else "text-red-400"
                    with ui.card().classes("bg-gray-800 rounded-lg p-2"):
                        ui.label(sl).classes("text-xs text-gray-300 font-semibold mb-1")
                        ui.label(f"{s['win_rate']}% WR  |  {s['count']} trades").classes(
                            "text-xs text-gray-400"
                        )
                        ui.label(f"${s['total_pnl']:+.2f}  PF {s['profit_factor']}").classes(
                            f"text-xs font-mono font-bold {pnl_cls}"
                        )

        # DPM detail
        d = data["dpm_detail"]
        if d["count"] > 0:
            ui.separator().classes("my-3")
            with ui.row().classes("gap-2 flex-wrap mb-2"):
                ui.label("DPM Detail").classes(
                    "text-xs text-gray-400 uppercase font-semibold"
                )
                ui.badge(f"Avg R: {d['avg_r_multiple']}", color="teal").classes("text-xs")
                if d["avg_trail_capture"] is not None:
                    ui.badge(
                        f"Trail capture: {d['avg_trail_capture']:.0%}", color="blue"
                    ).classes("text-xs")
                if d["calibrated_trades"] > 0:
                    ui.badge(
                        f"{d['calibrated_trades']} calibrated", color="green"
                    ).classes("text-xs")

            with ui.row().classes("gap-3 flex-wrap"):
                # Regime breakdown
                if d["regime_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Regime").classes("text-xs text-gray-500 uppercase mb-1")
                        for reg, rs in sorted(
                            d["regime_breakdown"].items(),
                            key=lambda x: x[1]["pnl"],
                            reverse=True,
                        ):
                            wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(reg).classes("text-xs text-gray-300 capitalize")
                                ui.label(f"${rs['pnl']:+.2f}  {wr}%").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )
                # Session breakdown
                if d["session_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Session").classes("text-xs text-gray-500 uppercase mb-1")
                        for sess, rs in sorted(
                            d["session_breakdown"].items(),
                            key=lambda x: x[1]["pnl"],
                            reverse=True,
                        ):
                            wr = round(rs["wins"] / rs["count"] * 100) if rs["count"] else 0
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(sess).classes("text-xs text-gray-300 capitalize")
                                ui.label(f"${rs['pnl']:+.2f}  {wr}%").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )
                # Exit type breakdown
                if d["exit_breakdown"]:
                    with ui.card().classes("bg-gray-800 rounded-lg p-3 flex-1 min-w-40"):
                        ui.label("Exit Type").classes("text-xs text-gray-500 uppercase mb-1")
                        for et, rs in sorted(
                            d["exit_breakdown"].items(),
                            key=lambda x: x[1]["count"],
                            reverse=True,
                        ):
                            pnl_cls = "text-green-400" if rs["pnl"] >= 0 else "text-red-400"
                            with ui.row().classes("items-center justify-between gap-1"):
                                ui.label(et).classes("text-xs text-gray-300")
                                ui.label(f"{rs['count']}×  ${rs['pnl']:+.2f}").classes(
                                    f"text-xs font-mono {pnl_cls}"
                                )

        # Claude analysis
        ui.separator().classes("my-3")
        verdict = analysis.get("overall_verdict", "")
        if verdict:
            ui.label(verdict).classes(
                "text-gray-200 text-xs leading-relaxed mb-2"
            )

        dpm_a = analysis.get("dpm_assessment", {})
        if dpm_a:
            with ui.card().classes("bg-teal-900/20 border border-teal-800 rounded-lg p-3"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("psychology", color="teal-300", size="xs")
                    ui.label("DPM Assessment").classes("text-teal-300 font-semibold text-xs")
                    if dpm_rec:
                        rec_label = {
                            "keep_dpm":           "Keep DPM",
                            "disable_dpm":        "Disable DPM",
                            "use_dpm_selectively": "Use Selectively",
                        }.get(dpm_rec, dpm_rec)
                        ui.badge(rec_label, color="teal").classes(f"text-xs {rec_cls}")
                if dpm_a.get("verdict"):
                    ui.label(dpm_a["verdict"]).classes("text-xs text-gray-300 mb-1 leading-relaxed")
                with ui.row().classes("gap-3 flex-wrap"):
                    if dpm_a.get("strength"):
                        with ui.column().classes("flex-1 min-w-32 gap-0"):
                            ui.label("Strength").classes("text-xs text-green-400 font-semibold")
                            ui.label(dpm_a["strength"]).classes("text-xs text-gray-400")
                    if dpm_a.get("weakness"):
                        with ui.column().classes("flex-1 min-w-32 gap-0"):
                            ui.label("Weakness").classes("text-xs text-red-400 font-semibold")
                            ui.label(dpm_a["weakness"]).classes("text-xs text-gray-400")
                if dpm_a.get("best_regime") or dpm_a.get("best_session"):
                    with ui.row().classes("gap-2 mt-1 flex-wrap"):
                        if dpm_a.get("best_regime"):
                            ui.badge(
                                f"Best regime: {dpm_a['best_regime']}", color="gray"
                            ).classes("text-xs")
                        if dpm_a.get("best_session"):
                            ui.badge(
                                f"Best session: {dpm_a['best_session']}", color="gray"
                            ).classes("text-xs")

        strat_notes = analysis.get("strategy_notes", [])
        if strat_notes:
            ui.separator().classes("my-2")
            ui.label("Fixed Strategy Notes").classes("text-xs text-gray-500 uppercase mb-1")
            for sn in strat_notes:
                sl = _APPROACH_LABELS.get(sn.get("strategy", ""), sn.get("strategy", ""))
                with ui.row().classes("items-start gap-2"):
                    ui.label(sl).classes("text-xs text-gray-300 font-semibold min-w-28")
                    ui.label(sn.get("verdict", "")).classes("text-xs text-gray-500 flex-1")

        if analysis.get("when_to_use_fixed"):
            ui.separator().classes("my-2")
            with ui.row().classes("items-start gap-2"):
                ui.icon("help_outline", color="gray-400", size="xs")
                ui.label("When to use fixed strategies: ").classes(
                    "text-xs text-gray-400 font-semibold"
                )
            ui.label(analysis["when_to_use_fixed"]).classes(
                "text-xs text-gray-400 leading-relaxed"
            )

        advice = analysis.get("actionable_advice", "")
        if advice:
            ui.separator().classes("my-2")
            with ui.card().classes("w-full bg-blue-900/20 border border-blue-800 rounded-lg p-3"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("lightbulb", color="blue-300", size="xs")
                    ui.label("Actionable Advice").classes("text-blue-300 font-semibold text-xs")
                ui.label(advice).classes("text-gray-200 text-xs leading-relaxed whitespace-pre-line")


# ── Recommended EA template, per channel (owner's request, 2026-09-02) ───────
#
# The picks already existed: strategy_ai.py has chosen from
# `auto_templates() + [STAND_DOWN]` since 2026-08-17, and stores one per
# channel in channel_strategy_rec. This page just never showed them -- it had
# a single free-text "Overall Recommendation" for everything, while its
# strategy/DPM panel still spoke in built-in strategy names.
#
# Display only. Nothing here applies a template; selecting one stays a
# deliberate act on Trading > Strategy.

_STAND_DOWN = "stand_down"
_TEMPLATE_PREFIX = "template:"


def _template_rec_rows(channels: list, recs: dict) -> list[dict]:
    """One row per channel, whether or not it has a recommendation.

    A channel with no pick is listed with a dash rather than omitted --
    dropping it would read as "this channel does not exist" rather than "the
    AI has not judged it yet".
    """
    rows: list[dict] = []
    for ch in channels:
        rec = recs.get(ch.get("source", "")) or {}
        raw = (rec.get("strategy") or "").strip()
        if not raw:
            label = "\u2014"
        elif raw == _STAND_DOWN:
            # A real recommendation -- trade nothing here -- not a missing one.
            label = "Stand down (trade nothing)"
        elif raw.startswith(_TEMPLATE_PREFIX):
            label = raw[len(_TEMPLATE_PREFIX):]
        else:
            label = raw
        rows.append({
            "channel":    ch.get("channel_name", ""),
            "template":   label,
            "confidence": rec.get("confidence") if raw else None,
            "reasoning":  (rec.get("reasoning") or "").strip(),
        })
    return rows


def _render_template_recs_panel(channels: list, recs: dict) -> None:
    rows = _template_rec_rows(channels, recs)
    if not rows:
        return
    with ui.card().classes("w-full bg-gray-900 p-4 mb-4"):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("auto_awesome", color="purple-300")
            ui.label("Recommended EA template by channel").classes(
                "text-purple-300 font-semibold text-sm")
        ui.label(
            "What the analysis suggests for each channel. Nothing is applied "
            "automatically \u2014 set it on Trading \u203a Strategy."
        ).classes("text-xs text-gray-500 mb-3")

        for r in rows:
            with ui.row().classes("items-start gap-2 w-full mb-2"):
                ui.label(r["channel"]).classes(
                    "text-xs font-semibold text-gray-300 shrink-0"
                ).style("width:9rem")
                with ui.column().classes("gap-0 flex-1 min-w-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(r["template"]).classes(
                            "text-xs font-mono "
                            + ("text-gray-500" if r["template"] == "\u2014"
                               else "text-purple-200")
                        )
                        if r["confidence"] is not None:
                            ui.badge(f"{float(r['confidence']):.0%}",
                                     color="purple").classes("text-xs")
                    if r["reasoning"]:
                        ui.label(r["reasoning"]).classes(
                            "text-xs text-gray-500 leading-snug")
