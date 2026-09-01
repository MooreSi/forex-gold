"""The risk card and its subcards: sizing, the circuit breaker, toxic hours,
internal exposure and daily profit management.

render_risk_card is public -- frontend/pages/trading/_strategy.py renders it
alongside the strategy settings -- and is re-exported from the package.
"""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl

_RISK_SUBCARD_CLASSES = "flex-1 min-w-72 bg-gray-800 p-3 rounded-lg"


def render_risk_card(card_classes: str = "w-full"):
    """Risk settings, split (2026-08-01) into five side-by-side sub-cards
    across two rows for the same tidy layout the rest of the Trading page
    uses (Strategy Parameters/Channel Strategy already sit side by side the
    same way) rather than one long scrolling card:

    Row 1: Risk Settings | Circuit Breaker | Toxic-Hour Blocklist
    Row 2: Internal Engine Exposure | Dynamic Position Management

    `card_classes` now sizes the OUTER wrapper (was previously the single
    inner ui.card()'s classes, back when this was one card) -- importable by
    other pages exactly as before, just laid out differently inside.

    Risk per trade (%) / Max risk per trade (%) and Fixed Lot Size all moved
    to Trading > Global Parameters (2026-07-24), so the two no longer need
    to be greyed in/out against each other from here.
    """
    rs = settings_ctl.get_risk_settings()

    with ui.column().classes(f"{card_classes} gap-4"):
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
            _render_risk_settings_subcard(rs)
            _render_circuit_breaker_subcard(rs)
            _render_toxic_hour_subcard(rs)

        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
            _render_internal_exposure_subcard(rs)
            _render_dpm_subcard(rs)


def _render_risk_settings_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("shield", size="sm").classes("text-yellow-400")
            ui.label("Risk Settings").classes("text-base font-bold text-yellow-300")

        # ── Risk Governor master toggle (Tier 1 safety layer) ─────────────────
        with ui.card().classes("w-full bg-gray-800 border border-yellow-600 mb-4 p-3"):
            with ui.row().classes("w-full items-center justify-between"):
                risk_gov = ui.switch(
                    "Risk Governor",
                    value=bool(rs.get("risk_governor_enabled", 0)),
                ).classes("text-yellow-300 font-bold")
                ui.icon("shield", size="sm").classes("text-yellow-400")
            with ui.expansion(
                "What does the Risk Governor do?", icon="info_outline"
            ).classes("w-full text-sm"):
                ui.markdown(
                    "When **ON**, a deterministic safety layer sits underneath *every* "
                    "strategy and applies these rules before and after each trade. It "
                    "never changes which strategy runs — only the size and frequency of "
                    "trades, so no single trade or bad day can blow up the account:\n\n"
                    "- **Risk-based position sizing** — lot size is calculated from "
                    "*Risk per trade (%)* and the actual stop distance instead of a fixed "
                    "lot. A wider stop is automatically sized smaller.\n"
                    "- **Hard per-trade ceiling** — no single trade may risk more than "
                    "*Max risk per trade (%)* of balance; trades that cannot fit are skipped.\n"
                    "- **Stop-width cap** — rejects any scalp whose stop is wider than "
                    "1.5x the current ATR.\n"
                    "- **Reward:risk floor** — blocks badly-inverted setups where the "
                    "first target is closer than a third of the stop distance (skipped "
                    "for strategies that set their own levels after fill, e.g. Conservative).\n"
                    "- **Directional cap** — at most 2 unprotected same-direction trades "
                    "open at once (no stacking into one side).\n"
                    "- **Daily-loss circuit breaker** — once today's realised losses reach "
                    "*Max daily loss (%)*, auto-trading pauses until the next trading day.\n\n"
                    "When **OFF**, none of the above runs and strategies behave exactly as "
                    "before. Toggle it to compare performance with and without the governor."
                ).classes("text-gray-300")

        ui.label(
            "Risk per trade % / Max risk per trade % moved to Trading > Global Parameters."
        ).classes("text-xs text-gray-500 italic mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            max_dd = ui.number(
                "Max daily loss (%)", value=float(rs.get("max_daily_loss_pct", 3.0)),
                min=0.1, max=100, step=0.5, format="%.1f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "If today's total losses exceed this percentage of balance, "
                "no new trades will be opened until the next trading day."
            )

        with ui.row().classes("w-full items-center gap-1"):
            max_tot_dd = ui.number(
                "Max total drawdown (%)", value=float(rs.get("max_total_drawdown_pct", 10.0)),
                min=0.1, max=100, step=1.0, format="%.1f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "If the account drops this percentage below its all-time high, "
                "all auto-execution is paused."
            )

        # ── Give-back guard ──────────────────────────────────────────────
        # Measures from the day's PEAK, not its opening balance, which is the
        # only way to express "protect the profit I had". Both limits above
        # measure from the open and so cannot see a day that goes +$349 and
        # closes -$88 -- which is what 2026-08-17 did.
        ui.separator().classes("my-3")
        with ui.row().classes("w-full items-center gap-1"):
            giveback_sw = ui.switch(
                "Stop for the day after giving back today's profit",
                value=bool(rs.get("giveback_guard_enabled", 0)),
            ).classes("text-sm text-blue-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Max daily loss measures from the day's OPENING balance, so a "
                "day that rises and then bleeds back never breaches it. This "
                "measures from the day's PEAK instead: once the day is up by "
                "the arming amount, handing back more than the set share of "
                "that peak stops new trades until the next broker day.\n\n"
                "Open trades are unaffected — this blocks new entries only, "
                "and Resume (or /resume) lifts it early.\n\n"
                "Unlike the limits above, this does NOT require the Risk "
                "Governor to be on."
            )
        with ui.row().classes("w-full items-center gap-2"):
            giveback_arm = ui.number(
                "Arm above profit ($)", value=float(rs.get("giveback_arm_usd", 50.0)),
                min=0, max=100000, step=10, format="%.0f",
            ).classes("flex-1").tooltip(
                "The guard only arms once the day's realised profit has reached "
                "this. Below it, ordinary churn around break-even can never lock "
                "the day out."
            )
            giveback_pct = ui.number(
                "Give-back limit (%)", value=float(rs.get("giveback_pct", 40.0)),
                min=1, max=99, step=5, format="%.0f",
            ).classes("flex-1").tooltip(
                "How much of the day's peak profit may be handed back before "
                "stopping. 40 = stop once 40% of the peak is gone."
            )
        giveback_arm.bind_visibility_from(giveback_sw, "value")
        giveback_pct.bind_visibility_from(giveback_sw, "value")

        with ui.row().classes("w-full items-center gap-1"):
            max_trades = ui.number(
                "Max open trades", value=int(rs.get("max_open_trades", 1)),
                min=1, max=10, step=1, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Maximum number of simultaneously open positions. "
                "New signals are ignored if this limit is reached."
            )

        with ui.row().classes("w-full items-center gap-1"):
            max_lot = ui.number(
                "Max lot size", value=float(rs.get("max_lot_size", 0.10)),
                min=0.01, max=100, step=0.01, format="%.2f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Absolute maximum lot size regardless of risk calculation. "
                "Prevents oversized trades in volatile conditions."
            )

        ui.separator().classes("my-3")
        excl_high = ui.checkbox(
            "Exclude High-Risk",
            value=bool(rs.get("exclude_high_risk", 0)),
        ).classes("text-sm text-gray-300")
        excl_high.tooltip(
            "When checked, any Telegram signal containing 'High Risk' is silently ignored and not traded."
        )

        def save_risk():
            try:
                settings_ctl.update_risk_settings({
                    "risk_governor_enabled":          int(bool(risk_gov.value)),
                    "max_daily_loss_pct":             float(max_dd.value      or 0),
                    "max_total_drawdown_pct":         float(max_tot_dd.value  or 0),
                    "max_open_trades":                int(max_trades.value    or 1),
                    "max_lot_size":                   float(max_lot.value     or 0),
                    "exclude_high_risk":              int(bool(excl_high.value)),
                    "giveback_guard_enabled":         int(bool(giveback_sw.value)),
                    "giveback_arm_usd":               float(giveback_arm.value or 0),
                    "giveback_pct":                   float(giveback_pct.value or 0),
                })
                ui.notify("Risk settings saved", type="positive")
            except (TypeError, ValueError) as _save_err:
                ui.notify(f"Invalid value — {_save_err}", type="negative")

        ui.button("Save Risk Settings", on_click=save_risk).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )


def _render_circuit_breaker_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon("block", size="sm").classes("text-red-400")
            ui.label("Circuit Breaker").classes("text-sm font-semibold text-red-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Blocks live MT5 trade execution after N consecutive losses. "
                "Signal engines and Telegram parsing continue — trades are queued "
                "and will activate once the cooldown expires."
            )

        cb_enabled = ui.checkbox(
            "Enable Circuit Breaker",
            value=bool(rs.get("circuit_breaker_enabled", 0)),
        ).classes("text-sm")

        with ui.row().classes("w-full items-center gap-2"):
            cb_losses = ui.number(
                "Consecutive losses to trigger",
                value=int(rs.get("circuit_breaker_losses", 3) or 3),
                min=1, max=20, step=1, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Number of consecutive losing live trades before the circuit breaker activates."
            )

        with ui.row().classes("w-full items-center gap-2"):
            cb_cooldown = ui.number(
                "Cooldown period (minutes)",
                value=int(rs.get("circuit_breaker_cooldown_mins", 60) or 60),
                min=1, max=1440, step=5, format="%.0f",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "How long (in minutes) live trading is blocked after the circuit breaker triggers."
            )

        def _cb_reset():
            settings_ctl.reset_circuit_breaker()
            ui.notify("Circuit breaker reset — live trading unblocked.", type="positive")

        with ui.row().classes("items-center gap-2 mt-1"):
            ui.button("Reset Circuit Breaker Now", on_click=_cb_reset).props("dense outline").classes(
                "text-xs text-red-300 border-red-600"
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Immediately clears an active circuit breaker and resets the loss counter."
            )

        def save_cb():
            try:
                settings_ctl.update_risk_settings({
                    "circuit_breaker_enabled":        int(bool(cb_enabled.value)),
                    "circuit_breaker_losses":         int(cb_losses.value     or 3),
                    "circuit_breaker_cooldown_mins":  int(cb_cooldown.value   or 60),
                })
                ui.notify("Circuit breaker settings saved", type="positive")
            except (TypeError, ValueError) as _save_err:
                ui.notify(f"Invalid value — {_save_err}", type="negative")

        ui.button("Save Circuit Breaker", on_click=save_cb).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )


def _render_toxic_hour_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        hour_block_val = bool(rs.get("hour_blocklist_enabled", 0))

        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("block").classes("text-red-400 text-base")
            ui.label("Toxic-Hour Blocklist").classes(
                "text-sm font-semibold text-red-300"
            )
            hour_block_badge = ui.badge(
                "BLOCKLIST ON" if hour_block_val else "BLOCKLIST OFF",
                color="red" if hour_block_val else "grey",
            )

        hour_block_chk = ui.checkbox(
            "Skip live execution during measured toxic hours",
            value=hour_block_val,
        ).classes("text-sm text-gray-200")

        def _hour_block_toggle(e):
            settings_ctl.update_risk_settings({"hour_blocklist_enabled": 1 if e.value else 0})
            hour_block_badge.props(f"color={'red' if e.value else 'grey'}")
            hour_block_badge.text = "BLOCKLIST ON" if e.value else "BLOCKLIST OFF"
            ui.notify(
                "Toxic-hour blocklist enabled" if e.value
                else "Toxic-hour blocklist disabled — all hours trade normally",
                type="positive" if e.value else "info",
            )

        hour_block_chk.on_value_change(_hour_block_toggle)

        ui.label(
            "Shared by the Bounce and Breakout generators. When OFF (default), "
            "every hour trades normally. When ON, the specific UTC hours each "
            "engine has measured as historically loss-making still generate and "
            "log signals as normal — so the ML models keep learning from real "
            "market data in those hours — they're just never placed as a real "
            "MT5 order."
        ).classes("text-xs text-gray-400 mt-1 leading-relaxed")


def _render_internal_exposure_subcard(rs: dict) -> None:
    from backend.src.controllers import settings_controller as _ieg

    with ui.card().classes(_RISK_SUBCARD_CLASSES):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("compare_arrows").classes("text-blue-400 text-base")
            ui.label("Internal Engine Exposure").classes(
                "text-sm font-semibold text-blue-300"
            )
            ui.icon("info_outline", size="xs").classes(
                "text-blue-400 cursor-help"
            ).tooltip(
                "Applies ONLY to the internal signal generators "
                "(Reversal, Breakout, Bounce). Telegram-channel trades "
                "are never affected by this setting."
            )
        ui.label(
            "The internal engines have no hedge guard of their own — each engine only "
            "blocks a duplicate in the SAME direction at the same level, and the "
            "cross-engine check ignores an engine's own signals. So a BUY and a SELL "
            "can sit open together. This controls whether that's allowed."
        ).classes("text-xs text-gray-500 mb-2")

        hedge_mode_sel = ui.select(
            {
                _ieg.MODE_OFF:          "Off — no restriction (default)",
                _ieg.MODE_SELF_HEDGE:   "Self-Hedge Guard — block opposing positions",
                _ieg.MODE_NET_EXPOSURE: "Net Exposure Cap — limit net directional lots",
            },
            value=(rs.get("internal_hedge_mode") or _ieg.MODE_OFF),
            label="Mode",
        ).classes("w-full").props("dense outlined")

        net_cap_num = ui.number(
            "Net exposure cap (lots)",
            value=float(rs.get("internal_net_exposure_max_lots", 0.30) or 0.30),
            min=0.0, step=0.01, format="%.2f",
        ).classes("w-full mt-1").props("dense outlined").tooltip(
            "Only used by Net Exposure Cap mode. Net = total BUY lots minus "
            "total SELL lots across all open internal-engine trades. "
            "0 = no cap (mode effectively disabled)."
        )

        with ui.column().classes("gap-0 mt-2"):
            ui.label("Off — no restriction (default)").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Long-standing behaviour, nothing is blocked. Opposing positions are "
                "allowed to open freely. Worth knowing before changing this: on 86 "
                "closed Reversal Engine trades (21–27 Jul), overlapping opposing pairs "
                "were 19% of trades but produced ~80% of total profit — 7 of 8 pairs "
                "had both legs win, and only one pair ever cancelled out (−$15.21). "
                "For a mean-reversion engine, buying support while selling resistance "
                "in a range is the strategy working, not a fault."
            ).classes("text-xs text-gray-500 mb-2")

            ui.label("Self-Hedge Guard").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Blocks a new internal-engine trade whenever ANY opposing-direction "
                "internal trade is already open. Strictest option: one direction at a "
                "time across all three engines combined. Best suited to trending "
                "conditions, where holding both sides tends to mean one leg is simply "
                "wrong. Costs you the range-play behaviour described above."
            ).classes("text-xs text-gray-500 mb-2")

            ui.label("Net Exposure Cap").classes(
                "text-xs font-semibold text-gray-300")
            ui.label(
                "Allows opposing positions but caps how far the book can lean one way. "
                "A hedge is always permitted because it REDUCES net exposure; what "
                "gets blocked is stacking further in whichever direction already "
                "dominates. Example at a 0.30 cap: net +0.20 long, a new 0.10 BUY "
                "takes it to +0.30 and is allowed; a further 0.10 BUY would reach "
                "+0.40 and is blocked — but a 0.10 SELL is allowed at any time, since "
                "it brings net back toward flat. The middle-ground option: keeps the "
                "range play, limits one-way pile-ups. A cap of 0 disables the check."
            ).classes("text-xs text-gray-500 mb-1")

        ui.label(
            "Blocked trades are skipped for live execution only — the signal is still "
            "generated, tracked, and used for learning, exactly like the other "
            "execution gates."
        ).classes("text-xs text-gray-600 italic mb-1")

        def save_strategy():
            try:
                _hm = hedge_mode_sel.value
                if isinstance(_hm, dict):
                    _hm = _hm.get("value")
                settings_ctl.update_risk_settings({
                    "internal_hedge_mode":            _hm or _ieg.MODE_OFF,
                    "internal_net_exposure_max_lots": float(net_cap_num.value or 0.30),
                })
                ui.notify("Settings saved", type="positive")
            except Exception as ex:
                ui.notify(str(ex), type="negative")

        ui.button("Save Settings", on_click=save_strategy).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
        )


def _render_dpm_subcard(rs: dict) -> None:
    with ui.card().classes(_RISK_SUBCARD_CLASSES):
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
            settings_ctl.update_risk_settings({"dpm_enabled": 1 if e.value else 0})
            dpm_badge.props(f"color={'blue' if e.value else 'grey'}")
            dpm_badge.text = "DPM ON" if e.value else "DPM OFF"
            ui.notify(
                "DPM enabled — strategy control handed off" if e.value
                else "DPM disabled — strategy selection restored",
                type="positive" if e.value else "info",
            )

        dpm_chk.on_value_change(_dpm_toggle)

        # ── DPM Profit Take ─────────────────────────────────────────────────
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
                        "Example: set $150 and the trade keeps running through "
                        "its normal TP levels until the running total hits "
                        "$150, then everything closes.\n"
                        "This applies whether or not Dynamic Position "
                        "Management is switched on — the check runs on every "
                        "open trade regardless. It sits under this heading for "
                        "grouping, not because it needs DPM.\n"
                        "0 = off (no dollar cap)."
                    )
                dpm_profit_inp = ui.number(
                    value=float(rs.get("profit_close_usd", 0.0) or 0.0),
                    min=0.0, step=5.0, format="%.2f",
                    placeholder="0 = off",
                ).classes("w-full")

            def _save_dpm_profit():
                try:
                    val = max(0.0, float(dpm_profit_inp.value or 0))
                    settings_ctl.update_risk_settings({"profit_close_usd": val})
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
