"""The strategy configuration cards: per-channel overrides, Strategy
Parameters, and the global parameter set."""
import asyncio
from datetime import datetime
from nicegui import ui
from backend.src.controllers import ai_controller as ai_provider
from backend.src.controllers import trading_controller as trading_ctl
from backend.src.controllers.trading_controller import (
    STRATEGY_NAMES,
)

import logging

_log = logging.getLogger(__name__)


def _render_channel_strategy_card(engine, all_names: dict, rs: dict) -> None:
    """
    Compact channel strategy card: one row per channel with name, stats badge,
    and strategy dropdown all inline.  Rec label lives in a tooltip on the
    psychology icon to avoid stacking extra height.
    """
    import asyncio as _aio
    from backend.src.controllers import trading_controller as _csai
    from backend.src.controllers import broker_controller as _et
    from backend.src.controllers import schedule_controller as _csched

    # Schedule Override banner (2026-08-06). While the Trading Schedule is
    # enabled, the active window's own per-channel strategy/template pick
    # wins over everything selected on this card, for as long as that window
    # is active -- see core_trading_schedule.get_schedule_strategy_override
    # and the two sites that honour it (core_signal_resolution.py and
    # core_scan_messages_staleness_strategy.py). Without this the card reads
    # as authoritative when it may not be, which is exactly the confusion
    # that made a schedule-assigned template look like it was being ignored.
    # Shown purely on the schedule's enabled flag, not on whether a window
    # happens to be active right now: this renders once on page load and
    # would otherwise go stale the moment a window boundary passed.
    if _csched.is_trading_schedule_enabled():
        with ui.row().classes(
            "w-full items-center gap-2 mb-2 px-2 py-1 rounded "
            "bg-amber-900 border-l-4 border-amber-500"
        ):
            ui.icon("event_available", size="xs").classes("text-amber-300")
            ui.label("Schedule Override").classes(
                "text-xs font-bold text-amber-200"
            )
            ui.icon("info_outline", size="xs").classes(
                "text-amber-300 cursor-help"
            ).tooltip(
                "The Trading Schedule is on. Where the active window sets a "
                "strategy or template for a channel, that wins over the pick "
                "below for as long as the window is active. Windows with no "
                "override configured leave the selection below in effect."
            )

    # Schedule Override banner (2026-08-06). While the Trading Schedule is
    # enabled, the active window's own per-channel strategy/template pick
    # wins over everything selected on this card, for as long as that window
    # is active -- see core_trading_schedule.get_schedule_strategy_override
    # and the two sites that honour it (core_signal_resolution.py and
    # core_scan_messages_staleness_strategy.py). Without this the card reads
    # as authoritative when it may not be, which is exactly the confusion
    # that made a schedule-assigned template look like it was being ignored.
    # Shown purely on the schedule's enabled flag, not on whether a window
    # happens to be active right now: this renders once on page load and
    # would otherwise go stale the moment a window boundary passed.
    if _csched.is_trading_schedule_enabled():
        with ui.row().classes(
            "w-full items-center gap-2 mb-2 px-2 py-1 rounded "
            "bg-amber-900 border-l-4 border-amber-500"
        ):
            ui.icon("event_available", size="xs").classes("text-amber-300")
            ui.label("Schedule Override").classes(
                "text-xs font-bold text-amber-200"
            )
            ui.icon("info_outline", size="xs").classes(
                "text-amber-300 cursor-help"
            ).tooltip(
                "The Trading Schedule is on. Where the active window sets a "
                "strategy or template for a channel, that wins over the pick "
                "below for as long as the window is active. Windows with no "
                "override configured leave the selection below in effect."
            )

    with ui.row().classes("items-center gap-2 mb-1"):
        ui.label("Channel Strategy").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Assign a strategy per channel. Auto lets Claude evaluate market "
            "conditions and update the recommendation every 30 min."
        )

    channels = [
        ch for ch in trading_ctl.get_all_channel_strategy_settings()
        # ORB/IVB isn't a channel -- it's a time-of-day breakout engine with
        # its own page and its own orb_fixed management, so a per-channel
        # strategy row for it doesn't belong in this list. It stays a
        # canonical source everywhere else (trade attribution, scorecards,
        # rename cascades) and any override already stored for it is still
        # honoured by orb_auto_execute.
        if ch["source"] != "ORB/IVB Report"
    ]

    strat_opts = {"": "— Inherit Global —", "auto": "Auto (Claude)"}
    strat_opts.update(STRATEGY_NAMES)
    for k, v in all_names.items():
        if k not in strat_opts:
            strat_opts[k] = v
    # EA Templates -- a saved template fully replaces strategy dispatch for
    # a channel (the EA manages the trade end-to-end), so it's a peer entry
    # in the same list rather than a second selector.
    for _t in _et.list_ea_templates():
        strat_opts[_et.override_for_template(_t["name"])] = f"Template: {_t['name']}"

    _rec_icons: dict[str, object] = {}   # source → ui.icon for tooltip updates
    _sel_map:   dict[str, object] = {}   # source → ui.select

    with ui.column().classes("w-full gap-1"):
        for ch in channels:
            src     = ch["source"]
            is_auto = ch.get("auto_strategy", False)
            cur_ov  = "auto" if is_auto else (ch.get("strategy_override") or "")
            rec     = trading_ctl.get_channel_strategy_rec(src)
            rec_tip = _rec_label_text(rec, strat_opts)

            with ui.row().classes("items-center gap-1 w-full"):
                ui.label(src).classes(
                    "text-xs font-semibold text-gray-300 truncate shrink-0"
                ).style("width:7rem").tooltip(src)
                sel = ui.select(
                    strat_opts, value=cur_ov, label=None,
                ).classes("text-xs min-w-0").style("flex:1").props("dense outlined")
                rec_icon = ui.icon("psychology", size="xs").classes(
                    "text-purple-400 cursor-help shrink-0"
                ).tooltip(rec_tip or "No recommendation yet — click Evaluate")
                _rec_icons[src] = rec_icon
                _sel_map[src]   = sel

            def _on_change(e, _src=src, _icon=rec_icon):
                _v = e.value
                if isinstance(_v, dict):  # NiceGUI dict-options returns {label,value} obj
                    _v = _v.get("value", "")
                val = (_v or "") if _v is not None else ""
                is_a = (val == "auto")
                override = None if (val in ("", "auto")) else val
                trading_ctl.set_channel_strategy_override(_src, override, auto=is_a)
                status = "Auto (Claude)" if is_a else (
                    f"Manual: {strat_opts.get(val, val)}" if val else "Inheriting global"
                )
                ui.notify(f"{_src}: {status}", type="info", timeout=2500)

            sel.on_value_change(_on_change)

    # ── Evaluate Now + auto-refresh ──────────────────────────────────────────
    ui.separator().classes("my-1 border-gray-700")

    eval_status = ui.label("").classes("text-xs text-gray-500")

    def _update_rec_tooltips(results: dict) -> None:
        for src, _r in results.items():
            if src in _rec_icons:
                new_rec = trading_ctl.get_channel_strategy_rec(src)
                tip = _rec_label_text(new_rec, strat_opts)
                _rec_icons[src].tooltip(tip or "No recommendation yet")

    async def _refresh_tooltips_from_db() -> None:
        """
        Cheap per-client poll (DB reads only, no Claude call) so tooltips reflect
        the engine's own singleton background evaluation loop. The actual Claude
        evaluation runs once per engine in _channel_ai_auto_eval_loop — this must
        never call evaluate_channels() itself, or duplicate browser tabs/reconnects
        would again multiply real API calls.
        """
        try:
            # Offloaded — see _render_live_lines (settings.py) for why a
            # per-channel sync DB call directly in a timer callback matters.
            recs = await trading_ctl.get_channel_strategy_recs(list(_rec_icons))
            for src, new_rec in recs.items():
                tip = _rec_label_text(new_rec, strat_opts)
                _rec_icons[src].tooltip(tip or "No recommendation yet")
            ts = __import__("datetime").datetime.now().strftime("%H:%M")
            eval_status.text = f"Updated {ts}"
        except Exception as e:
            _log.debug("[trading] strategy tooltip refresh failed: %s", e)

    def _apply_and_close(res: dict, dialog) -> None:
        for src, r in res.items():
            trading_ctl.set_channel_strategy_override(src, r["strategy"], auto=False)
            if src in _sel_map:
                _sel_map[src].value = r["strategy"]
        ui.notify("Recommendations applied to all channels", type="positive")
        dialog.close()

    async def _run_eval() -> None:
        """Called by the button — evaluates and shows results popup."""
        eval_status.text = "Evaluating…"
        cfg = engine._cfg if hasattr(engine, "_cfg") else {}
        try:
            results = await _csai.evaluate_channels(engine, cfg)
        except Exception as exc:
            eval_status.text = f"Failed: {exc}"
            ui.notify(f"Evaluation failed: {exc}", type="negative")
            return

        _update_rec_tooltips(results)
        ts = __import__("datetime").datetime.now().strftime("%H:%M")
        eval_status.text = f"Updated {ts}"
        used_ai = ai_provider.is_configured(cfg)
        _ai_label = "DeepSeek AI" if cfg.get("ai_provider") == "deepseek" else "Claude AI"

        # ── Results popup ────────────────────────────────────────────────────
        with ui.dialog().props("persistent") as dlg, \
             ui.card().classes("bg-gray-800 rounded-lg p-4 min-w-[480px] max-w-lg"):

            with ui.row().classes("items-center gap-2 mb-3"):
                ui.icon("psychology").classes("text-purple-400 text-xl")
                ui.label("Strategy Evaluation").classes(
                    "text-base font-bold text-yellow-300 flex-1"
                )
                ui.label(
                    f"{_ai_label if used_ai else 'Rule-based'} · {ts}"
                ).classes("text-xs text-gray-500")

            if not used_ai:
                ui.label(
                    "No AI provider configured — showing rule-based regime recommendations."
                ).classes("text-xs text-amber-400 italic mb-2")

            for src, r in results.items():
                strat_label = strat_opts.get(r["strategy"], r["strategy"])
                conf        = r.get("confidence", 0.0)
                reasoning   = r.get("reasoning", "")
                with ui.card().classes("bg-gray-900 rounded p-2 mb-1 w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(src).classes("text-xs font-semibold text-gray-200 flex-1")
                        ui.badge(strat_label, color="green").classes("text-xs")
                        ui.label(f"{conf:.0%}").classes("text-xs text-blue-300 font-mono")
                    if reasoning:
                        ui.label(reasoning).classes("text-xs text-gray-400 italic mt-0.5")

            ui.button(
                "Apply Recommendations", icon="check",
                on_click=lambda: _apply_and_close(results, dlg),
            ).classes("mt-3 bg-green-800 text-white text-xs w-full").props("dense")
            ui.button("Close", on_click=dlg.close).classes(
                "mt-1 text-xs w-full"
            ).props("flat dense")

        dlg.open()

    with ui.row().classes("items-center gap-2 mt-1"):
        ui.button(
            "Evaluate Now", icon="psychology",
            on_click=_run_eval,
        ).classes("text-xs bg-purple-800 text-white").props("dense").tooltip(
            "Ask Claude to evaluate current market conditions and recommend a strategy per channel. "
            "Results appear in a popup with an option to apply all at once."
        )
        eval_status

    ui.timer(60, _refresh_tooltips_from_db)
def _rec_label_text(rec: dict, strat_opts: dict) -> str:
    """Format the recommendation label under each channel dropdown."""
    strat = rec.get("strategy", "")
    reasoning = rec.get("reasoning", "")
    conf  = rec.get("confidence", 0.0)
    if not strat:
        return "No recommendation yet — click Evaluate Now"
    strat_name = strat_opts.get(strat, strat)
    conf_str   = f" ({conf:.0%})" if conf else ""
    reasoning_str = f" — {reasoning}" if reasoning else ""
    return f"Rec: {strat_name}{conf_str}{reasoning_str}"
def _render_strategy_params_card() -> None:
    """
    Strategy Parameters: live-editable SL/TP/close-% values for every
    fixed-parameter strategy (see core_strategy_params.PARAM_STRATEGIES),
    plus a small named-template library to save/reapply a parameter set
    later. A change here applies to the next trade opened under that
    strategy -- no restart, no code change. Mirrors a third-party EA's
    "Settings Templates" panel investigated 2026-07-22; see
    core_strategy_params.py's module docstring for why this needs no
    MQL5 changes at all (every strategy here is already fully resolved
    to concrete SL/TP prices by Python before any EA sees the trade).
    """
    from backend.src.controllers import strategy_controller as sp

    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("Strategy Parameters").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Live-editable SL/TP values for the fixed-parameter strategies -- "
            "a change applies to the next trade opened under that strategy, "
            "no restart needed. Save named presets below to switch between "
            "setups quickly."
        )

    state = {"strategy": sp.PARAM_STRATEGIES[0]}
    fields: dict[str, object] = {}

    def _on_strategy_change(e) -> None:
        v = e.value
        if isinstance(v, dict):  # NiceGUI dict-options returns {label,value} obj
            v = v.get("value")
        if v:
            state["strategy"] = v
            _draw_body()

    ui.select(
        sp.STRATEGY_LABELS, value=state["strategy"], label="Strategy",
    ).classes("w-56 mb-2").props("dense outlined").on_value_change(
        _on_strategy_change
    ).tooltip(
        "Which built-in strategy's fixed SL/TP point values to edit below. "
        "Changes apply to the next trade opened under that strategy."
    )

    body = ui.column().classes("w-full gap-2")

    def _current_values() -> dict:
        return {k: f.value for k, f in fields.items()}

    def _draw_body() -> None:
        body.clear()
        strategy = state["strategy"]
        specs = sp.PARAM_SPECS[strategy]
        live = sp.get_strategy_params(strategy)
        fields.clear()
        with body:
            with ui.row().classes("w-full gap-3 flex-wrap items-end"):
                for key, label, default, unit in specs:
                    step = 0.05 if unit in ("x", "frac") else 1.0
                    fields[key] = ui.number(
                        label=f"{label} ({unit})", value=live.get(key, default), step=step,
                        format="%.2f",
                    ).classes("w-36").props("dense outlined")

            with ui.row().classes("gap-2 mt-1"):
                ui.button("Save & Apply", on_click=_save_live).classes(
                    "text-xs bg-green-800 text-white"
                ).props("dense")
                ui.button("Reset to Default", on_click=_reset_default).classes(
                    "text-xs"
                ).props("dense outline")

            ui.separator().classes("my-2 border-gray-700")
            ui.label("Saved Templates").classes("text-sm font-semibold text-gray-300")

            templates = sp.list_templates(strategy)
            if not templates:
                ui.label("No saved templates for this strategy yet.").classes(
                    "text-xs text-gray-500"
                )
            else:
                for t in templates:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(t["name"]).classes(
                            "text-xs text-gray-200 truncate"
                        ).style("width:10rem").tooltip(t["name"])
                        ui.button("Apply", on_click=lambda _t=t: _apply_tpl(_t)).classes(
                            "text-xs"
                        ).props("dense flat color=blue")
                        ui.button(icon="delete_outline", on_click=lambda _t=t: _delete_tpl(_t)).props(
                            "dense flat color=red"
                        )

            with ui.row().classes("items-center gap-2 mt-2"):
                name_input = ui.input(placeholder="Template name").classes("w-48").props(
                    "dense outlined"
                )
                ui.button(
                    "Save as Template", on_click=lambda: _save_as_template(name_input)
                ).classes("text-xs").props("dense outline color=blue")

    def _save_live() -> None:
        strategy = state["strategy"]
        try:
            sp.set_strategy_params(strategy, _current_values())
            ui.notify(
                f"{sp.STRATEGY_LABELS[strategy]} parameters saved — applies to new trades",
                type="positive",
            )
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    def _reset_default() -> None:
        strategy = state["strategy"]
        sp.reset_strategy_params(strategy)
        ui.notify(f"{sp.STRATEGY_LABELS[strategy]} reset to defaults", type="info")
        _draw_body()

    def _apply_tpl(t: dict) -> None:
        try:
            sp.apply_template(t["id"])
            ui.notify(f"Applied template '{t['name']}'", type="positive")
            _draw_body()
        except Exception as exc:
            ui.notify(f"Apply failed: {exc}", type="negative")

    def _delete_tpl(t: dict) -> None:
        sp.delete_template(t["id"])
        ui.notify(f"Deleted template '{t['name']}'", type="info")
        _draw_body()

    def _save_as_template(name_input) -> None:
        strategy = state["strategy"]
        name = (name_input.value or "").strip()
        if not name:
            ui.notify("Enter a template name first", type="warning")
            return
        try:
            sp.save_template(strategy, name, _current_values())
            name_input.value = ""
            ui.notify(f"Saved template '{name}'", type="positive")
            _draw_body()
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    _draw_body()
def _render_global_parameters_card(rs: dict) -> None:
    """
    Global Parameters (2026-07-24): account-wide numbers that used to be
    scattered across the per-template EA Templates form, Active Strategy,
    and Risk Settings -- collected into one place since none of them are
    actually specific to a single template/strategy:

    - Harvest: moved from EA Templates' per-template harvest_enabled/
      harvest_threshold. Now applies to EVERY open position on the MT5
      account regardless of which strategy or template opened it (or
      whether it's EA-managed at all) -- pushed to the EA as a standing
      global config (ea_bridge.EABridge.push_global_config) rather than a
      per-trade field on open_trade, and the EA's OnTick sweeps every open
      position by ticket, not just the ones in its own g_trades[]/
      g_pending[] tracking. See ForexTraderBridge.mq5's
      CheckGlobalHarvest().
    - Fixed Lot Size (Single): moved from Active Strategy, same
      strategy_lot_size column/semantics (0 = risk-based auto) -- "fixed
      lot always wins" everywhere it's read (core_open_trade.py,
      core_fees_sizing.suggest_lot_size, core_manual_limit_order.py).
    - Fixed Lot Size (Grid): new. Used instead of the computed lot size
      for each leg of an EA Template in Grid mode -- see
      core_open_trade.py's template dispatch and
      ForexTraderBridge.mq5's HandleOpenTemplateGrid.
    - Risk per trade % / Max Risk per trade %: moved from Risk Settings,
      same risk_per_trade_pct/max_risk_per_trade_pct columns. Already fed
      into every strategy and template's lot sizing via
      core_fees_sizing.suggest_lot_size (risk_per_trade_pct as the base
      calculation, max_risk_per_trade_pct as an independent ceiling on
      top) -- moving them here is a pure UI relocation, no resolution
      logic changed.
    """
    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("Global Parameters").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Account-wide settings that apply to every strategy and template, "
            "regardless of which channel or EA Template opened the trade."
        )

    with ui.grid(columns=2).classes("w-full gap-3"):
        with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
            harvest_enabled = ui.switch(
                "Harvest", value=bool(rs.get("global_harvest_enabled", 0)),
            ).classes("text-sm")
            harvest_threshold = ui.number(
                "Profit threshold ($)", value=float(rs.get("global_harvest_threshold_usd", 50.0)),
                min=0.0, step=5.0,
            ).classes("w-full mt-1").props("dense outlined")
            ui.label(
                "Auto-close ANY open position (regardless of strategy, template, "
                "or how it was opened) once its own floating P&L reaches this "
                "amount."
            ).classes("text-xs text-gray-500 mt-1")

        with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
            fixed_lot_single = ui.number(
                "Fixed Lot Size (Single)", value=float(rs.get("strategy_lot_size", 0.0)),
                min=0.0, step=0.01, format="%.2f",
            ).classes("w-full").props("dense outlined")
            fixed_lot_single.tooltip(
                "Overrides risk-based sizing for every strategy and single-mode "
                "template. 0 = risk-based auto (Risk per trade %, below)."
            )
            fixed_lot_grid = ui.number(
                "Fixed Lot Size (Grid)", value=float(rs.get("strategy_lot_size_grid", 0.0)),
                min=0.0, step=0.01, format="%.2f",
            ).classes("w-full mt-2").props("dense outlined")
            fixed_lot_grid.tooltip(
                "Lot size used for EACH leg of an EA Template in Grid mode. "
                "0 = use the same lot as a normal (non-grid) trade."
            )

        with ui.card().classes("bg-gray-900 p-3 rounded-lg col-span-2"):
            with ui.row().classes("w-full gap-3"):
                risk_pct = ui.number(
                    "Risk per trade (%)", value=float(rs.get("risk_per_trade_pct", 0.5)),
                    min=0.01, max=100, step=0.1, format="%.2f",
                ).classes("flex-1").props("dense outlined")
                risk_pct.tooltip(
                    "Percentage of balance risked per trade — determines lot size "
                    "automatically when Fixed Lot Size is 0."
                )
                max_risk_pct = ui.number(
                    "Max Risk per trade (%)", value=float(rs.get("max_risk_per_trade_pct", 1.0)),
                    min=0.01, max=100, step=0.1, format="%.2f",
                ).classes("flex-1").props("dense outlined")
                max_risk_pct.tooltip(
                    "Hard ceiling — the risk-based lot size (above) is never allowed "
                    "to exceed this percentage of balance, regardless of Risk per "
                    "trade %. Does not apply when Fixed Lot Size is set."
                )

    def _save_global_params():
        try:
            trading_ctl.update_risk_settings({
                "global_harvest_enabled":       int(bool(harvest_enabled.value)),
                "global_harvest_threshold_usd": float(harvest_threshold.value or 0),
                "strategy_lot_size":             float(fixed_lot_single.value or 0),
                "strategy_lot_size_grid":        float(fixed_lot_grid.value or 0),
                "risk_per_trade_pct":            float(risk_pct.value or 0),
                "max_risk_per_trade_pct":        float(max_risk_pct.value or 0),
            })
            from backend.src.controllers import broker_controller as _ea_mod
            asyncio.create_task(_ea_mod.push_global_config())
            ui.notify("Global Parameters saved", type="positive")
        except Exception as ex:
            ui.notify(str(ex), type="negative")

    ui.button("Save Global Parameters", on_click=_save_global_params).classes(
        "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
    )
