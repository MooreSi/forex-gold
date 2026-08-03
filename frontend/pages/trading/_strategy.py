"""Strategy configuration: channel overrides, parameters, global settings
and EA templates."""
import json
from nicegui import ui
from backend.src.controllers.trading import controller as trading_ctl
from backend.src.utils.models import (
    STRATEGY_NAMES,
    STRATEGY_SCALE_OUT,
)
from frontend.pages.settings import render_risk_card

# Strategy id short-aliases, used by the comparison tables.
from backend.src.services.risk import strategy_params as _sp
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT as _SO, STRATEGY_BE_RUNNER as _BE,
    STRATEGY_TRAIL_STOP as _TS, STRATEGY_PROTECTED_SCALE as _PS,
    STRATEGY_CONSERVATIVE as _CO, STRATEGY_NO_SL_SCALE as _NSS,
    STRATEGY_CONSERVATIVE_TRIAL as _CT, STRATEGY_SCALP_RUNNER as _SR,
    STRATEGY_SIGNAL_CLIMBER as _SC,
    STRATEGY_REVERSAL_RUNNER as _RVR,
    STRATEGY_ADAPTIVE_RUNNER as _AR,
    STRATEGY_ADAPTIVE_RUNNER_2 as _AR2,
)
from ._ea_templates import _render_ea_templates_card
from ._strategy_cards import (
    _render_channel_strategy_card,
    _render_global_parameters_card,
    _render_strategy_params_card,
)


# Comparison-table cells. Callables rather than strings so a Strategy
# Parameters edit shows up on the next render -- see
# _render_strategy_params_card().
# Cells below that embed a live-tunable Strategy Parameters value are
# callables (evaluated fresh on every _draw_compare() render) instead of
# plain strings, so an edit on Trading > Strategy > Strategy Parameters is
# reflected here immediately -- see _render_strategy_params_card().
def _ct_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return (
        f"{p['tp1_pct']:g}% TP1 · {p['tp2_pct']:g}% TP2 · {p['tp3_pct']:g}% TP3 · "
        f"{p['tp4_pct']:g}% TP4 · {p['tp5_pct']:g}% TP5 · rest at TP6"
    )
def _ct_be_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return f"At TP2 (+{p['tp2_pt']:g} pts from fill)"
def _ct_max_upside_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return f"+{p['tp6_pt']:g} pts from fill price (TP6 fixed target)"
def _so_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_SO)
    return (
        f"{p['tp1_pct']:g}% TP1 · {p['tp2_pct']:g}% TP2 · {p['tp3_pct']:g}% TP3 · "
        f"{p['tp4_pct']:g}% TP4 · rest at last TP"
    )
def _ps_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_PS)
    return f"Yes — {p['mid_tp_close_pct']:g}% from TP3"
def _sc_be_cell() -> str:
    pos = int(_sp.get_strategy_params(_SC).get("be_at_pos", 1))
    return (
        f"After TP{pos} → entry (BE); after TP{pos + 1}+ → trails to previous TP price"
    )
def _be_filter_cell() -> str:
    thr = _sp.get_strategy_params(_BE)["adx_ranging_threshold"]
    return f"ADX > {thr:g} required — falls back to Scale Out in ranging markets"
def _be_best_market_cell() -> str:
    thr = _sp.get_strategy_params(_BE)["adx_ranging_threshold"]
    return f"Strong trend (ADX > {thr:g})"


# Strategy-comparison table data. Lived in the page module before the
# split; only this section reads it.
_PROTECTED_STRATS = frozenset({_SO, _BE, _TS, _PS, _CO, _SR, _SC, _RVR, _AR, _AR2})
_COMPARE_ROWS = [
    ("Partial closes", {
        _SO:  _so_partial_closes_cell,
        _BE:  "No",
        _TS:  "No",
        _PS:  _ps_partial_closes_cell,
        _CO:  "80% at TP1 (+3 pts from fill) · remainder trails via 3-pt stop",
        _NSS: "20% TP1 · 20% TP3 · rest at last TP (max TP8)",
        _CT:  _ct_partial_closes_cell,
        _SR:  "50% at TP1 (+3 pts from fill), SL untouched · remainder trails via 3-pt stop from TP2 (+4 pts)",
        _SC:  "20% TP1 · 15% TP2/3/4 · 20% TP5 · rest at TP6+ (signal's own TPs used)",
        _RVR: "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (signal's own TPs used)",
        _AR:  "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (same ladder as Reversal Runner, "
              "capped-widened SL — signal's own TPs used)",
        _AR2: "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (same ladder as Reversal Runner, "
              "fixed 10-pt SL — signal's own TPs used)",
    }),
    ("SL moves to BE", {
        _SO:  "After TP1",
        _BE:  "After TP1",
        _TS:  "Starts trailing at TP1",
        _PS:  "After TP2",
        _CO:  "Immediately at TP1 — SL moves to fill price (entry)",
        _NSS: "SL → TP1 at TP3 · steps TP{n-2} from TP4 onwards",
        _CT:  _ct_be_cell,
        _SR:  "At TP2 (+4 pts from fill) — SL moves to fill price (entry)",
        _SC:  _sc_be_cell,
        _RVR: "After TP1 → entry (BE); after TP2+ → trails to previous TP price",
        _AR:  "Immediately at TP1 → entry (BE); after TP2+ → trails to previous TP price "
              "(Reversal Runner waits until TP2 — Adaptive Runner doesn't need to, since its "
              "SL is already capped proportionate to the reachable reward)",
        _AR2: "At TP2 → entry (BE); after TP3+ → trails to the midpoint of the two TPs "
              "before the one just hit — not the single previous TP price every other "
              "ladder strategy uses",
    }),
    ("Max upside", {
        _SO:  "Capped at each TP",
        _BE:  "Highest TP",
        _TS:  "Unlimited (trend)",
        _PS:  "Capped from TP3",
        _CO:  "Unlimited (3-pt trailing stop after TP1)",
        _NSS: "Last TP of signal (max TP8)",
        _CT:  _ct_max_upside_cell,
        _SR:  "Unlimited (3-pt trailing stop after TP2 — full 50% runner)",
        _SC:  "Signal's final TP (TP6 typically 10-46 pts from entry on GD2 signals)",
        _RVR: "Signal's final TP (TP8 if present) — widened SL keeps the full ladder alive",
        _AR:  "Signal's final TP (TP8 if present) — SL widened only up to 50% of that "
              "distance, so the stop can never exceed the reward it's protecting",
        _AR2: "Signal's final TP (TP8 if present) — SL is a flat 10pts regardless of "
              "how far away that target actually is",
    }),
    ("Risk after TP1", {
        _SO:  "Zero (SL at entry from TP1)",
        _BE:  "Zero (SL at entry from TP1)",
        _TS:  "Trail distance only (trailing from TP1)",
        _PS:  "Full SL until TP2",
        _CO:  "Zero — trail floored at breakeven from TP1",
        _NSS: "1.5× emergency SL until TP3 (wide stop survives spikes)",
        _CT:  "Full SL until TP2",
        _SR:  "Full 10-pt SL until TP2, then zero — trail floored at breakeven",
        _SC:  "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _RVR: "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _AR:  "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _AR2: "Full 10-pt SL until TP2, then zero — SL locks to a two-TP-wide trailing "
              "midpoint after each subsequent level",
    }),
    ("Signal quality filter", {
        _SO:  "None",
        _BE:  _be_filter_cell,
        _TS:  "None",
        _PS:  "None",
        _CO:  "Direction only — signal SL/TP ignored entirely (5-pt SL / 3-pt TP1 from fill)",
        _NSS: "ADX > 30 required at entry — blocked in ranging/weak-trend conditions",
        _CT:  "Direction only — signal SL/TP ignored entirely",
        _SR:  "Direction only — signal SL/TP ignored entirely (10-pt SL / 3-pt TP1 / 4-pt TP2 from fill)",
        _SC:  "Full geometry validation — uses signal's SL and all TPs as-is",
        _RVR: "Full geometry validation — signal SL widened to min(4×, 20pt floor); TPs as-is",
        _AR:  "Full geometry validation — signal SL widened to min(4×, 20pt) then capped at "
              "50% of the final TP distance (never below the signal's own stated SL); TPs as-is",
        _AR2: "Direction + TP structure only — signal SL ignored entirely (fixed 10-pt SL "
              "from fill); TPs as-is",
    }),
    ("Best market", {
        _SO:  "Any",
        _BE:  _be_best_market_cell,
        _TS:  "Strong trend / breakout",
        _PS:  "Moderate trend / wider TPs",
        _CO:  "Any — tight scalp, quick TP1, small trail",
        _NSS: "Confirmed trend (ADX > 30) — GDV-style multi-TP signals",
        _CT:  "Any — fixed targets, low-maintenance",
        _SR:  "Any — tight scalp with two-stage confirmation before the 50% runner trails",
        _SC:  "Multi-TP professional signals (GD2, GDV) — built to ride the full TP ladder",
        _RVR: "Gold Diggers VIP zone-entry signals — built on 259-signal GDV backtest",
        _AR:  "Any multi-TP signal source of unknown/mixed ladder length — Gold Diggers VIP/"
              "GD2 and shorter-ladder channels alike; backtested 2026-07-15 against 226 real "
              "GDV/GD2 signals (+$400.29, PF 1.80, 5.8% max DD — lowest drawdown of every "
              "strategy tested there) and 309 Breakout/Bounce signals (still unprofitable "
              "there, like every strategy tested — that's an entry-quality issue, not "
              "something exit-strategy choice fixes)",
        _AR2: "Signals where a flat, predictable 10pt risk is preferred over the signal's "
              "own SL quality, and a two-level trail cushion is wanted instead of snapping "
              "to the immediately-prior TP — untested judgment call, not backtested",
    }),
]
# Table 1: core built-in strategies (left half)
_COMPARE_GROUP_1 = [_SO, _BE, _TS, _PS, _CO, _NSS]
# Table 2: advanced / specialised strategies + custom (right half)
_COMPARE_GROUP_2 = [_CT, _SR, _SC, _RVR, _AR, _AR2]


def _render_strategy(engine):
    outer = ui.column().classes("w-full gap-4")

    def _refresh():
        outer.clear()
        with outer:
            _draw()

    def _draw():  # noqa: C901  (complex but linear)
        rs            = trading_ctl.get_risk_settings()
        custom_strats = trading_ctl.get_custom_strategies()
        _hidden       = _get_hidden_strategies()
        custom_strats = [cs for cs in custom_strats if cs["id"] not in _hidden]
        custom_ids    = {cs["id"] for cs in custom_strats}

        # Resolve display ID (custom strategies have an extra display key)
        display_id = rs.get("display_strategy_id", "") or rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id.startswith("custom_") and display_id not in custom_ids:
            display_id = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id in _hidden:
            display_id = STRATEGY_SCALE_OUT
        _cur_strat = [display_id]

        all_names = {
            k: v for k, v in STRATEGY_NAMES.items()
            if k not in _hidden
        }
        all_names.update({cs["id"]: cs["name"] for cs in custom_strats})

        # ── Top row: Strategy Parameters (half) + Channel Strategy (half) ────
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

          # ── Strategy Parameters card ─────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_strategy_params_card()

          # ── Channel Strategy card ─────────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_channel_strategy_card(engine, all_names, rs)

        # ── Global Parameters card (full width) ────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_global_parameters_card(rs)

        # ── EA Templates card (full width) ────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_ea_templates_card()

        # ── 3-card row ────────────────────────────────────────────────────────
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):

            # ── Card 1: Active Strategy ───────────────────────────────────────
            with ui.card().classes("bg-gray-800 p-4 rounded-lg shrink-0 w-72"):
                ui.label("Active Strategy").classes("text-base font-bold text-yellow-300 mb-1")
                ui.label(
                    "Strategy is selected per-channel in Channel Strategy above. "
                    "The settings below apply regardless of which strategy is running."
                ).classes("text-xs text-gray-500 italic mb-3")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trailing Stop SL (pts)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Trailing Stop strategy only. "
                            "Initial stop-loss distance from entry price — gives the trade breathing room "
                            "before the trail activates at TP1."
                        )
                    trail_stop_sl = ui.number(
                        value=float(rs.get("trail_stop_sl_pts", 5.0)),
                        min=0.5, step=0.5, format="%.1f",
                    ).classes("w-full")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trailing Stop Distance (pts)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Trailing Stop strategy only. "
                            "TP1–TP8 are set at 1×–8× this distance from entry. "
                            "After TP1 is reached, the SL trails at this distance behind price."
                        )
                    trail_dist = ui.number(
                        value=float(rs.get("trailing_stop_distance", 5.0)),
                        min=0.1, step=0.5, format="%.1f",
                    ).classes("w-full")

                ui.separator().classes("my-2")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("ATR Collapse Threshold (0.0–1.0)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Suppresses signals when current ATR falls below this fraction of the recent "
                            "ATR baseline. 0.65 = block when volatility drops to 65% or below of normal. "
                            "Set to 0 to disable. Prevents trading in dead-market chop (Asian gaps, "
                            "pre-news consolidation) where spread costs eliminate edge."
                        )
                    atr_collapse_thresh = ui.number(
                        value=float(rs.get("atr_collapse_threshold", 0.65) or 0.65),
                        min=0.0, max=1.0, step=0.05, format="%.2f",
                    ).classes("w-full")

                kelly_enabled = ui.checkbox(
                    "Kelly Criterion Sizing",
                    value=bool(rs.get("kelly_sizing_enabled", 0)),
                ).classes("text-sm text-gray-300 mt-1")
                kelly_enabled.tooltip(
                    "Adjusts live-execution lot size using half-Kelly Criterion "
                    "based on rolling 50-trade win rate and R:R. "
                    "Multiplier is clamped to [0.75x, 1.25x] — modest adjustment only. "
                    "Requires ≥20 closed trades to activate."
                )

                def save_strategy():
                    try:
                        trading_ctl.update_risk_settings({
                            "trail_stop_sl_pts":       float(trail_stop_sl.value or 5.0),
                            "trailing_stop_distance":  float(trail_dist.value or 3.0),
                            "atr_collapse_threshold":  float(atr_collapse_thresh.value or 0.65),
                            "kelly_sizing_enabled":    int(kelly_enabled.value),
                        })
                        ui.notify("Settings saved", type="positive")
                    except Exception as ex:
                        ui.notify(str(ex), type="negative")

                ui.button("Save Settings", on_click=save_strategy).classes(
                    "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
                )

                # Auto-Execution moved to Parsing > Logic Keywords tab (2026-07-23).

                # ── Dynamic Position Management ───────────────────────────────
                ui.separator().classes("my-3")
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("psychology").classes("text-blue-400 text-base")
                    ui.label("Dynamic Position Management").classes(
                        "text-sm font-semibold text-blue-300"
                    )
                    dpm_enabled_val = bool(rs.get("dpm_enabled", 0))
                    dpm_badge = ui.badge(
                        "DPM ON" if dpm_enabled_val else "DPM OFF",
                        color="blue" if dpm_enabled_val else "grey",
                    )

                dpm_chk = ui.checkbox(
                    "Hand off to adaptive management",
                    value=dpm_enabled_val,
                ).classes("text-sm text-gray-200")

                def _dpm_toggle(e):
                    trading_ctl.update_risk_settings({"dpm_enabled": 1 if e.value else 0})
                    dpm_badge.props(f"color={'blue' if e.value else 'grey'}")
                    dpm_badge.text = "DPM ON" if e.value else "DPM OFF"
                    ui.notify(
                        "DPM enabled — strategy control handed off" if e.value
                        else "DPM disabled — strategy selection restored",
                        type="positive" if e.value else "info",
                    )

                dpm_chk.on_value_change(_dpm_toggle)

                # ── DPM Profit Take ───────────────────────────────────────────
                with ui.row().classes("items-end gap-2 mt-2 w-full"):
                    with ui.column().classes("flex-1 gap-0"):
                        with ui.row().classes("items-center gap-1"):
                            ui.label("Profit Take ($)").classes(
                                "text-xs text-gray-400 font-medium"
                            )
                            ui.icon("info_outline", size="xs").classes(
                                "text-blue-400 cursor-help"
                            ).tooltip(
                                "Close the remaining position when cumulative profit — "
                                "partial closes already taken plus unrealised P&L on "
                                "remaining lots — reaches this amount.\n"
                                "Example: set $150 and DPM will keep managing the trade "
                                "through its normal TP levels until the running total "
                                "hits $150, then close everything.\n"
                                "0 = DPM decides entirely (no dollar cap)."
                            )
                        dpm_profit_inp = ui.number(
                            value=float(rs.get("profit_close_usd", 0.0) or 0.0),
                            min=0.0, step=5.0, format="%.2f",
                            placeholder="0 = DPM decides",
                        ).classes("w-full")

                    def _save_dpm_profit():
                        try:
                            val = max(0.0, float(dpm_profit_inp.value or 0))
                            trading_ctl.update_risk_settings({"profit_close_usd": val})
                            if val > 0:
                                ui.notify(
                                    f"Profit take set to ${val:.2f} — DPM will close when "
                                    f"cumulative profit reaches this amount",
                                    type="positive",
                                )
                            else:
                                ui.notify(
                                    "Profit take cleared — DPM manages profit levels entirely",
                                    type="info",
                                )
                        except Exception as ex:
                            ui.notify(str(ex), type="negative")

                    ui.button("Set", on_click=_save_dpm_profit).classes(
                        "bg-blue-700 text-white px-3 text-xs"
                    ).style("height:30px; min-width:44px;")

                ui.label(
                    "Automatically adjusts trail distance, breakeven timing and "
                    "partial close size using ATR, session and momentum. "
                    "Set a Profit Take amount above to cap the cumulative target — "
                    "otherwise DPM decides entirely."
                ).classes("text-xs text-gray-400 mt-2 leading-relaxed")

                # Immediate Market Buy/Sell moved to Parsing > Logic Keywords tab (2026-07-23).

            # ── Card 1b: Risk Settings ──────────────────────────────────────────
            render_risk_card("bg-gray-800 p-4 rounded-lg shrink-0 w-72")

        # ── Quick comparison table ─────────────────────────────────────────────
        compare_container = ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-2")

        def _draw_compare():
            compare_container.clear()
            fresh_customs = trading_ctl.get_custom_strategies()
            _hidden_now   = _get_hidden_strategies()
            fresh_customs = [cs for cs in fresh_customs if cs["id"] not in _hidden_now]

            with compare_container:
                ui.label("Quick comparison").classes(
                    "text-sm font-bold text-gray-200 mb-3"
                )
                all_strat_nm = {
                    **{k: v for k, v in STRATEGY_NAMES.items() if k not in _hidden_now},
                    **{cs["id"]: cs["name"] for cs in fresh_customs},
                }
                active_strat = _cur_strat[0]

                # Build lookup for custom strategy comparison rows
                custom_cmp: dict[str, dict] = {}
                for cs in fresh_customs:
                    rules = json.loads(cs.get("rules_json") or "{}")
                    custom_cmp[cs["id"]] = {
                        "Partial closes": rules.get("partial_closes", "—"),
                        "SL moves to BE": rules.get("sl_moves_to_be", "—"),
                        "Max upside":     rules.get("max_upside", "—"),
                        "Risk after TP1": rules.get("risk_after_be", "—"),
                        "Signal quality filter": rules.get("signal_quality_filter", "—"),
                        "Best market":    rules.get("best_market", "—"),
                    }

                # Single shared confirmation dialog
                _pending: dict = {"sid": None, "sname": ""}
                with ui.dialog() as del_dialog, ui.card().classes(
                    "bg-gray-800 p-5 rounded-lg"
                ):
                    del_name_lbl = ui.label("").classes(
                        "text-gray-200 font-semibold mb-1"
                    )
                    ui.label("This cannot be undone.").classes(
                        "text-xs text-gray-400 mb-4"
                    )
                    with ui.row().classes("gap-2"):
                        def _do_del():
                            sid   = _pending["sid"]
                            sname = _pending["sname"]
                            if not sid:
                                return
                            if sid.startswith("custom_"):
                                trading_ctl.delete_custom_strategy(sid)
                            else:
                                _hide_builtin_strategy(sid)
                            del_dialog.close()
                            ui.notify(f"Strategy '{sname}' deleted", type="warning")
                            _refresh()
                        ui.button(
                            "Delete", icon="delete", on_click=_do_del,
                        ).classes("bg-red-700 text-white text-sm px-3 py-1")
                        ui.button(
                            "Cancel", on_click=del_dialog.close,
                        ).classes("bg-gray-700 text-white text-sm px-3 py-1")

                def _render_table(strats: list, label: str) -> None:
                    """Render one comparison grid for the given strategy list."""
                    if not strats:
                        return
                    ui.label(label).classes("text-xs font-semibold text-gray-500 mt-3 mb-1")
                    cols = "140px " + " ".join(["1fr"] * len(strats))
                    with ui.element("div").style(
                        f"display:grid;grid-template-columns:{cols};"
                        "border:1px solid #374151;border-radius:6px;overflow:hidden;"
                    ):
                        # Header row — row-label cell
                        ui.element("div").style("padding:8px 10px;background:#1e2433;")

                        # Header row — one cell per strategy
                        for strat in strats:
                            is_active    = (strat == active_strat)
                            is_deletable = strat not in _PROTECTED_STRATS
                            name         = all_strat_nm.get(strat, strat)
                            col_bg       = "background:#1e3a52;" if is_active else "background:#1e2433;"
                            col_fg       = "color:#38bdf8;" if is_active else "color:#9ca3af;"
                            cell_base    = (
                                f"padding:8px 10px;font-size:12px;font-weight:600;"
                                f"border-left:1px solid #374151;{col_bg}"
                            )

                            if is_deletable:
                                with ui.element("div").style(
                                    cell_base + "display:flex;align-items:center;"
                                    "justify-content:space-between;gap:4px;"
                                ):
                                    ui.label(name).style(
                                        f"font-size:12px;font-weight:600;{col_fg}"
                                        "margin:0;overflow:hidden;text-overflow:ellipsis;"
                                        "white-space:nowrap;min-width:0;"
                                    )
                                    def _open_del(s=strat, n=name):
                                        _pending["sid"]   = s
                                        _pending["sname"] = n
                                        del_name_lbl.text = f"Delete '{n}'?"
                                        del_dialog.open()
                                    ui.button(
                                        icon="delete_outline", on_click=_open_del,
                                    ).props("dense flat round size=xs").classes(
                                        "text-red-500 shrink-0"
                                    ).tooltip(f"Delete '{name}'")
                            else:
                                ui.label(name).style(cell_base + col_fg)

                        # Data rows
                        for i, (row_label, row_data) in enumerate(_COMPARE_ROWS):
                            bg = "#111827" if i % 2 == 0 else "#0f1117"
                            ui.label(row_label).style(
                                f"padding:8px 10px;background:{bg};"
                                "font-size:11px;color:#6b7280;font-weight:600;"
                                "border-top:1px solid #374151;"
                            )
                            for strat in strats:
                                is_active = (strat == active_strat)
                                val = (
                                    custom_cmp.get(strat, {}).get(row_label, "—")
                                    if strat.startswith("custom_")
                                    else row_data.get(strat, "—")
                                )
                                if callable(val):
                                    val = val()
                                ui.label(val).style(
                                    f"padding:8px 10px;background:{bg};"
                                    "font-size:11px;font-family:monospace;"
                                    "border-left:1px solid #374151;"
                                    "border-top:1px solid #374151;"
                                    + ("color:#93c5fd;" if is_active else "color:#e5e7eb;")
                                )

                # Table 1 — core strategies
                g1 = [s for s in _COMPARE_GROUP_1 if s not in _hidden_now]
                _render_table(g1, "Core strategies")

                # Table 2 — advanced / specialised strategies + custom
                g2 = [s for s in _COMPARE_GROUP_2 if s not in _hidden_now]
                g2 += [cs["id"] for cs in fresh_customs]
                _render_table(g2, "Advanced & specialised strategies")

        _draw_compare()

    _refresh()


def _get_hidden_strategies() -> set:
    raw = trading_ctl.get_app_config("hidden_strategies") or "[]"
    try:
        return set(json.loads(raw))
    except Exception:
        return set()
def _hide_builtin_strategy(sid: str) -> None:
    hidden = _get_hidden_strategies()
    hidden.add(sid)
    trading_ctl.set_app_config("hidden_strategies", json.dumps(sorted(hidden)))
