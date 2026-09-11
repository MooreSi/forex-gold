"""The capability switches from docs/todo/reversal-engine/200.

Migration 41 added fourteen columns to `vantage_risk_settings`, one per
capability, and they shipped with no UI -- so the only way to turn any of
them on was to edit the trading database by hand. A switch nobody can reach
is not a switch.

Every one of these is OFF and every default is byte-identical to the
behaviour it replaces, so this card changes nothing until somebody moves a
dial. Each one also changes how real money is traded, which is why the card
says so at the top and why the tooltips carry the measurement behind the
switch rather than a restatement of its name.

`backend/src/services/risk/capability_gates.py` is the only place these are
read; this is the only place they are written.
"""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl

_SUBCARD = "flex-1 min-w-72 bg-gray-800 p-3 rounded-lg"


def render_capabilities_subcard(rs: dict) -> None:
    with ui.card().classes(_SUBCARD):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon("science").classes("text-purple-400")
            ui.label("Reversal Engine Capabilities").classes(
                "text-sm font-bold text-purple-300")

        ui.label(
            "Every switch here is off and changes how real money is traded. "
            "Turn them on ONE AT A TIME on the demo account, and leave each "
            "one long enough to attribute -- two at once and neither can be "
            "credited or blamed. Run Signal Generator > Reversal Engine > "
            "Research first: it measures what these are meant to fix."
        ).classes("text-xs text-amber-300 mb-3 leading-relaxed")

        # ── Entry geometry ───────────────────────────────────────────
        ui.label("Entry geometry").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")

        atr_barriers = ui.checkbox(
            "Size the stop and targets from volatility",
            value=bool(rs.get("re_atr_barriers_enabled", 0)),
        ).classes("text-sm text-gray-300")
        atr_barriers.tooltip(
            "The engine's stop varies 4-7 points with level score while its "
            "targets are fixed point offsets, so TP1 is 0.75R on a weak level "
            "and 0.43R on a strong one -- the better the level, the worse the "
            "payoff. With this on, the stop is ATR x the multiple below and "
            "the target ladder is rescaled so TP1 lands at the same multiple, "
            "which makes R constant. Both numbers below are PROVISIONAL "
            "starting values; replace them with the fitted figures the "
            "Research study produces."
        )
        atr_stop = ui.number(
            "Stop, as a multiple of ATR",
            value=float(rs.get("re_atr_stop_mult", 1.2) or 1.2),
            min=0.2, max=5.0, step=0.1, format="%.2f",
        ).classes("w-full")
        atr_tp1 = ui.number(
            "First target, as a multiple of ATR",
            value=float(rs.get("re_atr_tp1_mult", 1.2) or 1.2),
            min=0.2, max=10.0, step=0.1, format="%.2f",
        ).classes("w-full")

        ui.separator().classes("my-3")

        # ── Confirmation ─────────────────────────────────────────────
        ui.label("Confirmation at the level").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")

        trigger_on = ui.checkbox(
            "Require confirmation, not just arrival",
            value=bool(rs.get("entry_trigger_enabled", 0)),
        ).classes("text-sm text-gray-300")
        trigger_on.tooltip(
            "A signal currently fires when price comes within range of a "
            "ranked level, with nothing asked about HOW it arrived. Price "
            "arriving fast at a level is the case where the level is about to "
            "fail: measured on this account, fills inside five minutes are "
            "453 trades at -$1,950. Tick at least one check below, or this "
            "requires nothing and confirms everything."
        )
        trigger_rejection = ui.checkbox(
            "…price must trade through the level and close back inside it",
            value=bool(rs.get("entry_trigger_rejection", 0)),
        ).classes("text-sm text-gray-300 ml-4")
        trigger_decel = ui.checkbox(
            "…price must be arriving slowly",
            value=bool(rs.get("entry_trigger_deceleration", 0)),
        ).classes("text-sm text-gray-300 ml-4")
        trigger_ratio = ui.number(
            "Slow means: mean bar range under this fraction of ATR",
            value=float(rs.get("entry_trigger_max_range_ratio", 0.5) or 0.5),
            min=0.1, max=3.0, step=0.1, format="%.2f",
        ).classes("w-full")

        ui.separator().classes("my-3")

        # ── The learned gate ─────────────────────────────────────────
        ui.label("The learned gate").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")

        meta_on = ui.checkbox(
            "Ask the meta-labeller before trading",
            value=bool(rs.get("meta_label_gate_enabled", 0)),
        ).classes("text-sm text-gray-300")
        meta_on.tooltip(
            "A second model that answers only 'will this trade clear its own "
            "execution cost', trained on purged and embargoed folds. It "
            "REFUSES to arm until it can beat a coin out of sample, and an "
            "unarmed model blocks nothing -- so turning this on before the "
            "Research study has costed a few hundred trades does nothing at "
            "all, which is deliberate."
        )
        meta_threshold = ui.number(
            "Refuse below this probability",
            value=float(rs.get("meta_label_threshold", 0.5) or 0.5),
            min=0.0, max=1.0, step=0.05, format="%.2f",
        ).classes("w-full")

        ui.separator().classes("my-3")

        # ── Standing aside ───────────────────────────────────────────
        ui.label("When to stand aside").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")

        liquidity_on = ui.checkbox(
            "Skip the rollover, the weekend reopen and period end",
            value=bool(rs.get("session_liquidity_gate_enabled", 0)),
        ).classes("text-sm text-gray-300")
        liquidity_on.tooltip(
            "Illiquidity that arrives on a clock rather than a calendar, so "
            "no news filter can see it: the daily rollover around 22:00 UTC "
            "when gold spreads go to several times normal, and the Sunday "
            "reopen which carries the weekend gap. Period end is defined but "
            "left off inside this gate until the attribution says it costs "
            "anything."
        )
        events_on = ui.checkbox(
            "Widen the news blackout for the events that matter",
            value=bool(rs.get("event_tier_gate_enabled", 0)),
        ).classes("text-sm text-gray-300")
        events_on.tooltip(
            "The existing blackout gives FOMC and a middling trade-balance "
            "print the same window. This tiers them -- 60 minutes before and "
            "90 after for a rate decision, CPI or payrolls, down to 10 and 20 "
            "for everything else. Wider AFTER than before on purpose: the "
            "spread is worst and stops are reached in the minutes following "
            "a release, not the minutes before it."
        )

        ui.separator().classes("my-3")

        # ── Sizing and levels ────────────────────────────────────────
        ui.label("Sizing and levels").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")

        vol_sizing = ui.checkbox(
            "Scale size by volatility and drawdown",
            value=bool(rs.get("vol_target_sizing_enabled", 0)),
        ).classes("text-sm text-gray-300")
        vol_sizing.tooltip(
            "The same percent risk on a violent day is more risk, not the "
            "same risk. Shrinks size as ATR rises above its reference and as "
            "drawdown deepens past 5%, never below a quarter of normal. "
            "NOT YET CONNECTED to the order path -- the policy is built and "
            "tested, and wiring it is a demo-session decision. Turning this "
            "on today records the intent and changes no lot size."
        )
        corr_cap = ui.number(
            "Cap total correlated exposure at this many lots (0 = off)",
            value=float(rs.get("correlated_exposure_cap_lots", 0.0) or 0.0),
            min=0.0, max=100.0, step=0.01, format="%.2f",
        ).classes("w-full")

        levels_on = ui.checkbox(
            "Add the institutional reference levels",
            value=bool(rs.get("liquidity_map_levels_enabled", 0)),
        ).classes("text-sm text-gray-300")
        levels_on.tooltip(
            "Previous day high/low/close and midpoint, previous week's range, "
            "the daily and weekly open, the session's initial balance, VWAP, "
            "and the point of control and value area of the session's volume "
            "profile. They join the existing candidate list and are scored by "
            "the same rules, at a deliberately low provisional strength so "
            "they cannot outrank a level whose edge has actually been "
            "measured. This makes the engine consider MORE levels, so expect "
            "more signals."
        )

        def _save() -> None:
            try:
                settings_ctl.update_risk_settings({
                    "re_atr_barriers_enabled":       int(bool(atr_barriers.value)),
                    "re_atr_stop_mult":              float(atr_stop.value or 1.2),
                    "re_atr_tp1_mult":               float(atr_tp1.value or 1.2),
                    "entry_trigger_enabled":         int(bool(trigger_on.value)),
                    "entry_trigger_rejection":       int(bool(trigger_rejection.value)),
                    "entry_trigger_deceleration":    int(bool(trigger_decel.value)),
                    "entry_trigger_max_range_ratio": float(trigger_ratio.value or 0.5),
                    "meta_label_gate_enabled":       int(bool(meta_on.value)),
                    "meta_label_threshold":          float(meta_threshold.value or 0.5),
                    "session_liquidity_gate_enabled": int(bool(liquidity_on.value)),
                    "event_tier_gate_enabled":       int(bool(events_on.value)),
                    "vol_target_sizing_enabled":     int(bool(vol_sizing.value)),
                    "correlated_exposure_cap_lots":  float(corr_cap.value or 0.0),
                    "liquidity_map_levels_enabled":  int(bool(levels_on.value)),
                })
                ui.notify("Capability switches saved", type="positive")
            except (TypeError, ValueError) as err:
                ui.notify(f"Invalid value — {err}", type="negative")

        ui.button("Save Capabilities", on_click=_save).classes(
            "bg-purple-700 text-white mt-3 px-4 py-2")
