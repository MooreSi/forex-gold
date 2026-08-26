"""The ML metrics and trade-history sections of the breakout panel.

Both were nested renderers inside render(), each closing over exactly one
name -- the container it draws into -- so they lift out as functions of that
container. Bodies are verbatim.
"""
from nicegui import ui

from backend.src.controllers import engines_controller

from ._shared import (
    _bo_type_badge,
    _dir_color,
    _fmt_dur,
    _fmt_ts,
    _ml_thresh,
    _outcome_color,
    _pnl_color,
    _pnl_str,
)
async def _render_history(*, history_area) -> None:
    sigs   = await engines_controller.breakout.all_signals(limit=80)
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
                        "Realized R", "ADX", "H1 Bias", "Strategy", "Outcome",
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
                    # Realized R -- actual outcome relative to the risk this
                    # signal actually took (sl_dist), not the static TP1-vs-SL
                    # plan ratio (rr_tp1) computed once at signal time. Every
                    # row in this table already has a decided outcome (this
                    # list is pre-filtered to non-pending/triggered signals),
                    # so realized R is always available and always more
                    # informative than the fixed planned ratio, which never
                    # varies from signal to signal since TP1 is itself derived
                    # as sl_dist x a constant multiplier. rr_tp1 stays
                    # untouched as the ML pipeline's own R-multiple label.
                    sl_dist     = sig.get("sl_dist")
                    realized_rr = (
                        float(pnl_pts) / float(sl_dist)
                        if pnl_pts is not None and sl_dist else None
                    )
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
                            (f"{realized_rr:+.1f}R" if realized_rr is not None else "—", "text-blue-300"),
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


async def _render_ml(*, ml_area) -> None:
    s    = await engines_controller.breakout.ml_summary()
    mets = await engines_controller.breakout.ml_metrics()
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

            needed   = _ml_thresh['min_train_samples']
            have     = s.get("labeled_count", 0)
            next_in  = max(0, needed - have) if not s.get("trained") else \
                       _ml_thresh['retrain_every'] - (have % _ml_thresh['retrain_every'] or _ml_thresh['retrain_every'])
            next_str = f"+{next_in}" if s.get("trained") else f"{have}/{needed}"
            _chip("Next Train", next_str, "text-cyan-300",
                  f"Retrains every {_ml_thresh['retrain_every']} new labeled examples "
                  f"once {_ml_thresh['min_train_samples']} minimum reached.")

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
                f"Need {_ml_thresh['min_train_samples']} closed signals with ML probability stored."
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
