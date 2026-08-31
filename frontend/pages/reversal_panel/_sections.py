"""The ML/learning and trade-history sections of the reversal panel.

Both were try-blocks inside _refresh_all, a 497-line closure over 20 names.
They are the two that closed over exactly ONE of those names -- the container
they render into -- so they lift out as functions of that container without
turning twenty closure variables into twenty parameters. Bodies are verbatim,
dedented one level.
"""
import logging

from backend.src.controllers import engines_controller as engines_controller
from nicegui import ui

_log = logging.getLogger(__name__)

from ._shared import (
    _dir_color,
    _fmt_duration,
    _fmt_ts,
    _level_type_badge,
    _ml_thresh,
    _outcome_color,
    _pnl_color,
    _pnl_str,
)
async def _render_history_section(history_container) -> None:
    try:
        all_sigs = await engines_controller.reversal.all_signals(limit=80)
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
    except Exception as e:
        _log.debug("[reversal panel] signal history table refresh failed: %s", e)

async def _render_ml_section(ml_container) -> None:
    try:
        ml_sum = await engines_controller.reversal.ml_summary()
        mets   = await engines_controller.reversal.ml_metrics()
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

                needed  = _ml_thresh['min_train_samples']
                have    = ml_sum.get("labeled_count", 0)
                next_in = max(0, needed - have) if not ml_sum.get("trained") else \
                          _ml_thresh['retrain_every'] - (have % _ml_thresh['retrain_every'] or _ml_thresh['retrain_every'])
                next_str = f"+{next_in}" if ml_sum.get("trained") else f"{have}/{needed}"
                _chip("Next Train", next_str, "text-cyan-300",
                      f"Retrains every {_ml_thresh['retrain_every']} new labeled examples "
                      f"once {_ml_thresh['min_train_samples']} minimum reached.")

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
                    f"No calibration data yet. Need {_ml_thresh['min_train_samples']} "
                    f"closed signals with ML probability stored."
                ).classes("text-gray-600 text-xs italic mt-1")

            # Feature list
            ui.label("Features:").classes("text-xs text-gray-500 mt-2")
            feat_txt = ", ".join(ml_sum.get("features", []))
            ui.label(feat_txt).classes("text-xs text-gray-600 leading-relaxed")
    except Exception as e:
        _log.debug("[reversal panel] ML status section refresh failed: %s", e)
