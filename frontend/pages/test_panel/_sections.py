"""The active-signals, trade-history and ML-metrics sections of the Bounce panel.

All three were nested renderers inside _render_main(), each closing over
exactly one name -- the container it draws into -- so they lift out as
functions of that container. Verified before moving rather than assumed:
nothing else in any of the three is free. Bodies are verbatim.

Same shape as frontend/pages/breakout_panel/_sections.py.
"""
from nicegui import ui

from backend.src.controllers import engines_controller

from ._shared import (
    _dir_color,
    _fmt_duration,
    _fmt_ts,
    _ml_thresh,
    _outcome_color,
    _pnl_color,
    _pnl_str,
    _status_badge_color,
)


async def _render_active(*, active_area) -> None:
    sigs = await engines_controller.bounce.open_signals()
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
            _ood_feat   = engines_controller.bounce.ml_features_for_signal(sig["id"])
            _ood_dist   = engines_controller.bounce.ood_distance(_ood_feat) if _ood_feat else None

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

async def _render_history(*, history_area) -> None:
    sigs   = await engines_controller.bounce.all_signals(limit=100)
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

async def _render_ml(*, ml_area) -> None:
    s    = await engines_controller.bounce.ml_summary()
    mets = await engines_controller.bounce.ml_metrics()
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
            _needed  = _ml_thresh['min_train_samples']
            _next_in = s["next_train_in"]
            _next_str = f"+{_next_in}" if s["trained"] else f"{_have}/{_needed}"
            _chip("Next Train", _next_str, "text-cyan-300",
                  f"Retrains every {_ml_thresh['retrain_every']} new labeled examples "
                  f"once {_ml_thresh['min_train_samples']} minimum reached. "
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
                f"Need {_ml_thresh['min_train_samples']} closed signals with ML probability stored."
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
        regime_rows = engines_controller.bounce.perf_by_regime()
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
