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

from frontend.components import learning_chart as _learning_chart

_log = logging.getLogger(__name__)

from ._shared import (
    _dir_color,
    _fmt_duration,
    _fmt_ts,
    _level_type_badge,
    _live_exec_badge,
    _ml_thresh,
    _outcome_color,
    _pnl_color,
    _pnl_str,
)
async def _render_history_section(history_container, diff=None) -> None:
    """`diff` is a `SectionCache`, or None to rebuild unconditionally.

    The clear moved in here from the caller along with the guard, because the
    two have to agree: clearing outside and skipping inside would empty the
    table and leave it empty. Pure over `closed` -- every cell comes off a row
    and the formatting helpers are pure. bugs/030.
    """
    try:
        all_sigs = await engines_controller.reversal.all_signals(limit=80)
        closed   = [s for s in all_sigs if s.get("status") == "closed"][:60]
        if diff is not None and not diff.changed("history", closed):
            return
        history_container.clear()

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
        # The digest is stored before the render, so a throw part-way through
        # would leave this container half-built and the cache calling it done.
        if diff is not None:
            diff.forget("history")
        _log.debug("[reversal panel] signal history table refresh failed: %s", e)

async def _render_ml_section(ml_container, diff=None) -> None:
    """`diff` is a `SectionCache`, or None to rebuild unconditionally.

    Pure over `(ml_sum, mets)`. The clear moved in here with the guard, for
    the same reason as the history section above. bugs/030.
    """
    try:
        ml_sum = await engines_controller.reversal.ml_summary()
        mets   = await engines_controller.reversal.ml_metrics()
        if diff is not None and not diff.changed("ml", (ml_sum, mets)):
            return
        ml_container.clear()
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
            # Rolling window, labelled axes, shared with the Breakout panel.
            # Both panels carried a byte-identical copy of this chart and a
            # cumulative mean that could not move; see learning_chart.py.
            sig_ids      = mets.get("signal_ids", [])
            pred_r_ser   = mets.get("pred_r_series", [])
            actual_r_ser = mets.get("actual_r_series", [])
            win_rates    = mets.get("win_rate_series", [])
            _learning_chart.render(mets)

            if sig_ids:
                n = len(sig_ids)
                last_n = min(5, n)
                with ui.element("table").classes("w-full text-xs mt-1"):
                    with ui.element("thead"):
                        with ui.element("tr").classes("text-gray-600 border-b border-gray-800"):
                            for h_label in ["Signal", "Win%", "Pred R", "Act R"]:
                                with ui.element("th").classes("text-left px-1 py-0.5"):
                                    ui.label(h_label)
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
        # The digest is stored before the render, so a throw part-way through
        # would leave this container half-built and the cache calling it done.
        if diff is not None:
            diff.forget("ml")
        _log.debug("[reversal panel] ML status section refresh failed: %s", e)


def _render_research_section(container) -> None:
    """The research study card: one button, one report.

    On demand rather than on refresh. The study reads broker tick history
    for every closed trade it measures, which is minutes of round trips --
    not something to run on a panel timer. Synchronous because it only
    BUILDS the card; the button's own handler is what awaits the study.

    It places nothing, closes nothing and changes no setting. What it
    writes is two measurement columns on rows that had none.
    """
    with container:
        ui.label("Research study").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")
        ui.label(
            "Reconstructs how far each closed trade actually travelled from "
            "broker tick history, prices what each fill cost, attributes the "
            "results by cohort, and fits the stop and target to what price "
            "did. Reads only; it places nothing."
        ).classes("text-xs text-gray-500 mb-2")

        output = ui.label("").classes(
            "text-xs font-mono whitespace-pre text-gray-300 overflow-x-auto")

        async def _run() -> None:
            button.disable()
            output.set_text("Running. This reads tick history per trade and "
                            "takes a few minutes.")
            try:
                output.set_text(
                    await engines_controller.reversal_research_study())
            except Exception as e:                # noqa: BLE001
                _log.warning("[reversal panel] research study failed: %s", e)
                output.set_text(f"The study could not complete: {e}")
            finally:
                button.enable()

        button = ui.button("Run study", icon="science", on_click=_run) \
            .classes("text-xs bg-slate-700 text-white px-3") \
            .props("dense unelevated")

        shadow_box = ui.column().classes("w-full mt-3")

        def _shadow() -> None:
            shadow_box.clear()
            rows = engines_controller.reversal_shadow_report()
            with shadow_box:
                ui.label("Champion vs challenger").classes(
                    "text-xs font-semibold text-gray-400 uppercase tracking-wider")
                for r in sorted(rows, key=lambda x: not x["is_champion"]):
                    mean_r = "no decisions yet" if r["mean_r"] is None \
                        else f"{r['mean_r']:+.3f}R"
                    label = r["variant"] + (" (live)" if r["is_champion"] else "")
                    ui.label(f"{label}: took {r['n_taken']}, skipped "
                             f"{r['n_skipped']}, {mean_r}, "
                             f"net {r['net']:+.2f}").classes(
                        "text-xs text-gray-400 font-mono")

        ui.button("Shadow report", icon="compare_arrows", on_click=_shadow) \
            .classes("text-xs bg-slate-800 text-white px-3 mt-2") \
            .props("dense unelevated")


def render_open_signal_card(sig: dict) -> None:
    """One open reversal signal, in the Breakout panel's card shape.

    Owner request 2026-09-16. The old card was a left-border strip with a
    flat row of six numbers; this is the Breakout card's layout -- direction
    block, price line, context line, reference/status column -- carrying the
    Reversal Engine's own evidence rather than the Breakout engine's.

    The difference is not cosmetic. Breakout decides on a broken level, ADX
    and a quality score. Reversal decides on a LEVEL and its SCORE, gates on
    an ML probability, and correlates against the reference channel. Those
    are the fields on the card, and `level_score` in particular is the number
    `capability_gates` blocks on -- without it the card cannot say why the
    signal exists.

    The entry stays a RANGE. A reversal signal is a zone, and collapsing it
    to the single price the Breakout card shows would misreport the setup.

    Every field is read defensively: `get_open_signals` is `SELECT *` on a
    table that has gained columns five times, and a card that raises takes
    the whole tab down with it.
    """
    def _f(key, default=0.0):
        try:
            v = sig.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    direction = str(sig.get("direction") or "")
    is_buy    = direction.upper() == "BUY"
    border    = "border-green-800" if is_buy else "border-red-800"
    dir_bg    = "bg-green-800" if is_buy else "bg-red-900"
    status    = str(sig.get("status") or "pending")
    sig_ref   = str(sig.get("signal_ref") or "")
    badge_text, badge_color = _level_type_badge(str(sig.get("level_type") or ""))

    with ui.card().classes(f"w-full bg-gray-800 rounded-lg p-4 border {border}"):
        with ui.row().classes("w-full items-start gap-4"):
            with ui.column().classes(
                f"rounded-lg px-3 py-2 {dir_bg} items-center min-w-16"
            ):
                ui.label(direction or "?").classes(
                    f"text-sm font-bold {_dir_color(direction)}")
                ui.label("XAUUSD").classes("text-xs text-gray-400")

            with ui.column().classes("flex-1 gap-1"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.badge(badge_text, color=badge_color).classes("text-xs")
                    lo, hi = _f("entry_low"), _f("entry_high")
                    if lo is not None and hi is not None:
                        ui.label(f"${lo:.2f}–${hi:.2f}").classes(
                            "text-white font-semibold")
                    sl = _f("stop_loss")
                    if sl is not None:
                        moved = " (moved)" if sig.get("sl_moved_to_be") else ""
                        ui.label(f"SL ${sl:.2f}{moved}").classes("text-red-300 text-xs")
                    for key, label, cls in (("tp1", "TP1", "text-green-300"),
                                            ("tp3", "TP3", "text-green-400"),
                                            ("tp7", "TP7", "text-green-500")):
                        v = _f(key)
                        if v:
                            ui.label(f"{label} ${v:.2f}").classes(f"{cls} text-xs")
                    rr = _f("rr_tp1")
                    if rr:
                        ui.label(f"R:R {rr:.1f}:1").classes(
                            "text-blue-300 text-xs font-mono")

                with ui.row().classes("items-center gap-2 flex-wrap"):
                    lvl = _f("level_price")
                    if lvl:
                        ui.label(
                            f"Level: ${lvl:.2f} ({sig.get('level_type') or 'level'})"
                        ).classes("text-orange-300 text-xs")
                    score = _f("level_score")
                    if score is not None:
                        ui.label(f"Score {score:.2f}").classes(
                            "text-yellow-300 text-xs font-mono")
                    prob = _f("ml_prob")
                    if prob is not None:
                        ui.label(f"ML {prob:+.3f}").classes(
                            "text-purple-300 text-xs font-mono")
                    adx = _f("adx")
                    if adx:
                        ui.label(f"ADX {adx:.1f}").classes(
                            "text-purple-300 text-xs font-mono")
                    ui.label(f"HTF: {sig.get('htf_bias') or '?'}").classes(
                        "text-gray-400 text-xs")
                    ui.label(f"H1: {sig.get('h1_bias') or '?'}").classes(
                        "text-gray-400 text-xs")
                    ui.label(f"Session: {sig.get('session') or '?'}").classes(
                        "text-gray-400 text-xs")
                    _corr = "confirmed" if sig.get("correlation_confirmed") else "none"
                    ui.label(f"REF corr: {_corr}").classes("text-gray-400 text-xs")

            with ui.column().classes("items-end gap-1 shrink-0"):
                if sig_ref:
                    ui.label(sig_ref).classes("text-xs font-mono text-gray-500")
                ui.label(status.upper()).classes("text-xs font-mono text-blue-300")
                _exec = _live_exec_badge(str(sig.get("live_exec_status") or ""))
                if _exec:
                    _bt, _bc, _tip = _exec
                    ui.badge(_bt, color=_bc).classes("text-xs").tooltip(_tip)
