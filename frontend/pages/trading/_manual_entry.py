import logging
"""Manual entry: signal form, market order form, and the ORB/IVB report.

These place real orders. The forms collect and validate; execution goes
through the trading controller -- see docs/system/rules/20-trading-safety.md.
"""
import asyncio
from nicegui import ui
from backend.src.controllers import trading_controller as trading_ctl
from backend.src.controllers.trading_controller import is_stuck_placeholder
from backend.src.controllers.trading_controller import (
    STRATEGY_NAMES,
    STRATEGY_SCALE_OUT,
    STRATEGY_ORB_FIXED,
)
from backend.src.controllers.trading_controller import validate_signal
from backend.src.controllers import sync_controller as sync_ctl

# Sibling sections of this page.
from ._shared import _stat_cell

log = logging.getLogger(__name__)


def _render_signal_entry(engine):
    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Create Limit Order").classes("text-lg font-bold text-yellow-300 mb-4")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "BUY = expecting price to rise (long). SELL = expecting price to fall (short)."
                )
            direction = ui.select(["BUY", "SELL"], value="BUY").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Entry Low").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The lower price of your entry zone. The trade opens when current price is within "
                    "this range. For a single entry price, set both Low and High to the same value."
                )
            entry_low = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Entry High").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The upper price of your entry zone. Must be >= Entry Low. "
                    "For a single entry price, set both Low and High to the same value."
                )
            entry_high = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Stop Loss").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The price at which the trade is automatically closed at a loss to protect your "
                    "account. For BUY trades: place below entry. For SELL trades: place above entry."
                )
            stop_loss = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.expansion("Take Profit Levels", icon="add").classes("w-full bg-gray-700 rounded mt-3"):
            ui.label(
                "TP levels are checked in order. The strategy determines what happens at each "
                "level (partial close, SL move, etc). At minimum set TP1."
            ).classes("text-xs text-gray-500 p-2")
            with ui.grid(columns=3).classes("gap-3 p-1"):
                _tp_defs = [
                    ("TP1", "First target. Most strategies close a portion here and move SL to breakeven."),
                    ("TP2", "Second target. Remaining position continues after TP1 is hit."),
                    ("TP3", "Third target. Conservative strategy skips this level."),
                    ("TP4", "Fourth target. Conservative strategy closes 10% here and steps up SL."),
                    ("TP5", "Fifth target."),
                    ("TP6", "Sixth target."),
                    ("TP7", "Seventh target."),
                    ("TP8", "Final target — Conservative strategy exits at TP7 (second-to-last); TP8 is headroom only."),
                ]
                _tp_inputs = []
                for _label, _tip in _tp_defs:
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-0.5"):
                            ui.label(_label).classes("text-xs text-gray-400 font-medium")
                            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(_tip)
                        _tp_inputs.append(ui.number(value=None, format="%.2f").classes("w-full"))
                tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8 = _tp_inputs

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Lot Size").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Set to 0 to auto-calculate from your Risk % setting and the entry-to-SL distance. "
                    "Enter a fixed value (e.g. 0.05) to override automatic sizing."
                )
            lot_size = ui.number(value=0.0, min=0.0, step=0.01, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Notes (optional)").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Free text notes about this signal — e.g. source channel, setup reason, or context."
                )
            notes = ui.input().classes("w-full")
        status_lbl = ui.label("").classes("text-sm text-red-300 mt-1")

        def _validate() -> list[str]:
            return validate_signal(
                direction.value, entry_low.value or 0, entry_high.value or 0,
                stop_loss.value or 0,
                tp1.value or None, tp2.value or None, tp3.value or None,
                tp4.value or None, tp5.value or None,
                tp6.value or None, tp7.value or None, tp8.value or None,
            )

        async def submit():
            status_lbl.text = ""
            errors = _validate()
            if errors:
                status_lbl.text = " | ".join(errors)
                return
            try:
                sig = engine.create_signal(
                    source_name="Manual", direction=direction.value,
                    entry_low=entry_low.value or 0, entry_high=entry_high.value or 0,
                    stop_loss=stop_loss.value or 0,
                    tp1=tp1.value or None, tp2=tp2.value or None, tp3=tp3.value or None,
                    tp4=tp4.value or None, tp5=tp5.value or None,
                    tp6=tp6.value or None, tp7=tp7.value or None, tp8=tp8.value or None,
                    lot_size=lot_size.value if lot_size.value else None,
                    notes=notes.value,
                )
                ui.notify(
                    f"Signal saved — see Pending Signals tab to execute it.",
                    type="positive",
                )
                asyncio.create_task(_background_commentary(engine, sig["signal_id"]))
            except Exception as e:
                status_lbl.text = str(e)

        async def submit_and_open():
            # Places a genuine broker-side resting BuyLimit/SellLimit via the
            # EA (core_manual_limit_order.py) -- fixed 2026-07-24. Previously
            # routed through open_trade_from_signal(), the same "wait for
            # price to re-enter the zone, fill at MARKET" path the automatic
            # Telegram zone-signal handler uses, which only worked when price
            # already happened to sit inside Entry Low-High at the moment of
            # the click and rejected with "price is above/below the entry
            # zone" otherwise -- backwards for a page whose whole point is to
            # place an order that rests until price gets there.
            status_lbl.text = ""
            errors = _validate()
            if errors:
                status_lbl.text = " | ".join(errors)
                return
            try:
                result = await engine.open_manual_limit_order(
                    direction=direction.value,
                    entry_low=entry_low.value or 0, entry_high=entry_high.value or 0,
                    stop_loss=stop_loss.value or 0,
                    tp1=tp1.value or None, tp2=tp2.value or None, tp3=tp3.value or None,
                    tp4=tp4.value or None, tp5=tp5.value or None,
                    tp6=tp6.value or None, tp7=tp7.value or None, tp8=tp8.value or None,
                    lot_size=lot_size.value if lot_size.value else None,
                    notes=notes.value,
                )
                ui.notify(
                    f"Limit order placed @ {result['price']:.2f} — EA ticket {result['mt5_ticket']}",
                    type="positive",
                )
            except Exception as e:
                status_lbl.text = str(e)
                ui.notify(str(e), type="negative")

        with ui.row().classes("gap-2 mt-4"):
            ui.button("Save Signal", on_click=submit).classes("bg-blue-700 text-white px-4 py-2")
            ui.button("Place Limit Order", on_click=submit_and_open).classes(
                "bg-green-700 text-white px-4 py-2"
            )

    ui.label(
        "Save Signal records a zone-watch signal (Pending Signals tab) that fills at market once "
        "price returns to the zone. Place Limit Order sends a genuine resting BuyLimit/SellLimit "
        "order to the broker via the EA immediately — requires the EA bridge connected and healthy."
    ).classes("text-xs text-gray-500 mt-2 max-w-2xl")
def _render_market_order_form(engine):
    """Immediate market entry: choose direction, optional SL/lot override, hit the button."""

    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        # ── Header ────────────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.icon("bolt").classes("text-amber-400 text-xl")
            ui.label("Market Order").classes("text-lg font-bold text-yellow-300")

        # Live price display
        price_label = ui.label("Current price: fetching...").classes(
            "text-sm text-gray-300 bg-gray-700 px-3 py-1 rounded mb-4 w-fit"
        )

        # Strategy / DPM status banner
        rs_now   = trading_ctl.get_risk_settings()
        dpm_on   = bool(rs_now.get("dpm_enabled", 0))
        strat_nm = STRATEGY_NAMES.get(rs_now.get("trade_strategy", ""), "Scale Out")
        mode_txt = "DPM" if dpm_on else strat_nm
        mode_col = "text-blue-300" if dpm_on else "text-amber-300"
        mode_lbl = ui.label(f"Active mode: {mode_txt}").classes(
            f"text-xs font-semibold {mode_col} mb-3"
        )

        # ── Direction ─────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "BUY = go long (expecting price to rise). SELL = go short (expecting price to fall)."
                )
            mo_direction = ui.select(["BUY", "SELL"], value="BUY").classes("w-full")

        # ── Stop Loss ─────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Stop Loss (optional when DPM is ON)").classes(
                    "text-xs text-gray-400 font-medium"
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The price at which the trade will be closed at a loss. "
                    "If left at 0 and DPM is enabled, an ATR-based stop is set automatically. "
                    "Required when DPM is OFF."
                )
            mo_sl = ui.number(
                value=0.0, min=0.0, step=0.5, format="%.2f",
                placeholder="0 = auto (DPM only)",
            ).classes("w-full")

        # ── Lot Size ──────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Lot Size (optional)").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Leave at 0 to auto-size from your Risk % and the stop distance. "
                    "Enter a fixed value to override."
                )
            mo_lot = ui.number(
                value=0.0, min=0.0, max=10.0, step=0.01, format="%.2f",
                placeholder="0 = auto",
            ).classes("w-full")

        # ── Strategy ──────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-4"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Strategy").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Strategy applied to this manual market order. "
                    "Overrides the global Active Strategy for this trade only."
                )
            _mo_strat_names = {
                k: v for k, v in STRATEGY_NAMES.items()
                if k != STRATEGY_ORB_FIXED
            }
            _mo_rs = trading_ctl.get_risk_settings()
            _mo_default = _mo_rs.get("trade_strategy", STRATEGY_SCALE_OUT)
            mo_strategy = ui.select(
                _mo_strat_names,
                value=_mo_default if _mo_default in _mo_strat_names else STRATEGY_SCALE_OUT,
            ).classes("w-full")

        status_lbl = ui.label("").classes("text-sm text-red-300 mb-2")

        async def _enter_market():
            status_lbl.text = ""
            # "disabled" is not a real Quasar QBtn prop (it's "disable") and,
            # even if it were, HTML's disabled attribute disables on mere
            # presence regardless of its string value — so a later
            # .props("disabled=false") never actually re-enabled the button.
            # .disable()/.enable() set the real boolean prop correctly.
            btn.props("loading=true")
            btn.disable()
            try:
                sl_val  = float(mo_sl.value  or 0)
                lot_val = float(mo_lot.value or 0)
                _sl     = sl_val  if sl_val  > 0 else None
                _lot    = lot_val if lot_val > 0 else None
                _strat  = mo_strategy.value or None

                if sync_ctl.is_remote_active():
                    # This node is stood down (VPS is the active trader) — route
                    # the order to the VPS's own account instead of just
                    # failing with "Trading stood down". Mirrors the Signal
                    # Generator panels' remote Start/Stop/Run Now pattern.
                    ack = await sync_ctl.send_market_order(
                        direction=mo_direction.value,
                        stop_loss=_sl, lot_size=_lot, strategy=_strat,
                    )
                    if ack.get("error"):
                        raise RuntimeError(ack["error"])
                    result = ack.get("result") or {}
                else:
                    result = await engine.open_manual_market_order(
                        direction=mo_direction.value,
                        stop_loss=_sl,
                        lot_size=_lot,
                        strategy=_strat,
                    )
                entry = float(result.get("entry_price", 0))
                ticket = result.get("mt5_ticket", "—")
                ui.notify(
                    f"{mo_direction.value} opened @ {entry:.2f}  |  ticket {ticket}",
                    type="positive",
                )
            except Exception as exc:
                status_lbl.text = str(exc)
                ui.notify(str(exc), type="negative")
            finally:
                btn.props("loading=false")
                btn.enable()

        btn = ui.button(
            "Enter at Market Price",
            on_click=_enter_market,
        ).classes("bg-amber-600 hover:bg-amber-500 text-white w-full py-3 text-base font-semibold mt-2")

        # Refresh live price and active mode on a short timer
        async def _refresh_price():
            try:
                tick = await engine.get_tick()
                if tick:
                    bid, ask = tick.bid, tick.ask
                    price_label.text = (
                        f"Current price:  Bid {bid:.2f}  |  Ask {ask:.2f}  "
                        f"|  Spread {tick.spread_points:.0f} pts"
                    )
                rs_r  = await trading_ctl.get_risk_settings_async()
                d_on  = bool(rs_r.get("dpm_enabled", 0))
                s_nm  = STRATEGY_NAMES.get(rs_r.get("trade_strategy", ""), "Scale Out")
                m_txt = "DPM" if d_on else s_nm
                m_col = "text-blue-300 text-xs font-semibold mb-3" if d_on \
                        else "text-amber-300 text-xs font-semibold mb-3"
                mode_lbl.text = f"Active mode: {m_txt}"
                mode_lbl.classes(replace=m_col)
            except Exception:
                pass

        ui.timer(3.0, _refresh_price)

    ui.label(
        "Places an order immediately at the current market price using your active "
        "strategy and risk settings. With DPM enabled, a stop is auto-calculated from ATR "
        "and DPM manages the trade from there."
    ).classes("text-xs text-gray-500 mt-2 max-w-2xl")
def _render_orb_report(engine):
    """London opening-range-breakout report — classic ORB methodology
    (https://www.litefinance.org/blog/for-beginners/trading-strategies/opening-range-breakout-strategy/):
    the whole Asian session (00:00-08:00 UTC) is a confirmation filter, the
    first 15 minutes of London (08:00-08:15 UTC) is the traded opening
    range, a breakout only counts once price clears BOTH in the same
    direction. Stop at the opening range's midpoint, target at 2x the
    resulting risk (auto-executed as a genuine market order once
    confirmed) with an informational-only 3x level shown alongside it."""
    import base64
    from backend.src.controllers import notifications_controller as email_service

    ui.button("Refresh", icon="refresh", on_click=lambda: refresh()).props("flat").classes(
        "text-gray-400 mb-2"
    )
    container = ui.column().classes("w-full gap-3")

    async def refresh():
        container.clear()
        try:
            report = await engine.build_orb_report()
        except Exception as e:
            with container:
                ui.label(f"Failed to build ORB report: {e}").classes("text-red-400 text-sm p-4")
            return

        with container:
            if not report:
                ui.label(
                    "No ORB report available yet — this builds from the whole Asian "
                    "session plus the first 15 minutes of London, and is only "
                    "available from London open onward."
                ).classes("text-gray-500 text-sm italic p-4")
                return

            direction = report.get("direction", "inside")
            _label = {"bullish": ("BREAKOUT — BULLISH", "text-green-400"),
                      "bearish": ("BREAKOUT — BEARISH", "text-red-400"),
                      "unconfirmed": ("BROKE OPENING RANGE — UNCONFIRMED", "text-amber-400"),
                      "inside": ("INSIDE RANGE", "text-gray-400")}
            status_txt, status_col = _label.get(direction, ("—", "text-gray-400"))

            with ui.card().classes("w-full max-w-3xl bg-gray-800 p-6 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.icon("candlestick_chart").classes("text-amber-400 text-xl")
                    ui.label("London Open — ORB Report").classes("text-lg font-bold text-yellow-300")

                with ui.row().classes("items-center gap-4 mb-3"):
                    ui.label(f"Current price: ${float(report.get('current_price', 0) or 0):.2f}").classes(
                        "text-sm text-gray-300 bg-gray-700 px-3 py-1 rounded"
                    )
                    ui.label(status_txt).classes(f"text-sm font-bold {status_col}")

                if report.get("phase") == "forming":
                    ui.label(report.get("position_note", "")).classes("text-sm text-gray-400 italic mb-2")
                    _stat_cell(
                        "ASIAN RANGE (00:00–08:00 UTC)",
                        f"${report['asia_low']:.2f} – ${report['asia_high']:.2f}  "
                        f"({report['asia_range']:.1f} pts)",
                    )
                    return

                # Asian range (filter) + London opening range (traded range)
                with ui.grid(columns=2).classes("w-full text-sm gap-2 mb-2"):
                    _stat_cell(
                        "ASIAN RANGE (00:00–08:00 UTC)",
                        f"${report['asia_low']:.2f} – ${report['asia_high']:.2f}  "
                        f"({report['asia_range']:.1f} pts)",
                    )
                    _stat_cell(
                        "LONDON OPENING RANGE (08:00–08:15 UTC)",
                        f"${report['or_low']:.2f} – ${report['or_high']:.2f}  "
                        f"({report['or_range']:.1f} pts)",
                    )

                if report.get("position_note"):
                    ui.label(report["position_note"]).classes("text-xs text-gray-500 mb-3")

                # Chart
                try:
                    chart_png = email_service.build_orb_chart_image(report)
                except Exception as e:
                    chart_png = None
                    log.warning("ORB chart render failed: %s", e)
                if chart_png:
                    b64 = base64.b64encode(chart_png).decode()
                    ui.image(f"data:image/png;base64,{b64}").classes("w-full rounded mb-3")

                # Breakout entry/exit setup, once confirmed
                if direction in ("bullish", "bearish"):
                    rr = report.get("rr")
                    target2 = report.get("target2")

                    ui.label("Breakout Setup").classes(
                        "text-xs font-semibold text-amber-300 uppercase tracking-wide mt-2 mb-1"
                    )
                    with ui.grid(columns=4).classes("w-full text-sm gap-2 mb-1"):
                        _stat_cell("STOP", f"${report['stop']:.2f}", "text-red-400")
                        _stat_cell("TARGET (2:1)", f"${report['target']:.2f}", "text-green-400")
                        _stat_cell(
                            "TARGET 2 (3:1, info only)",
                            f"${target2:.2f}" if target2 else "—", "text-green-400",
                        )
                        _stat_cell("R:R", f"{rr:.2f}:1" if rr else "—", "text-amber-300")
                    ui.label(
                        "Stop = midpoint of the London opening range. Target = 2x the "
                        "resulting risk (auto-executed). Target 2 = 3x risk, shown for "
                        "reference only — the automated path closes fully at Target, "
                        "it does not manage a partial-close ladder."
                    ).classes("text-xs text-gray-500 mb-3")

                    mt5_direction = "BUY" if direction == "bullish" else "SELL"
                    status_lbl = ui.label("").classes("text-sm text-red-300 mb-2")

                    async def _execute_orb(mt5_direction=mt5_direction, report=report):
                        status_lbl.text = ""
                        exec_btn.props("loading=true")
                        exec_btn.disable()
                        try:
                            _lot_val = float(lot_inp.value or 0)
                            _lot = _lot_val if _lot_val > 0 else None
                            if sync_ctl.is_remote_active():
                                ack = await sync_ctl.send_market_order(
                                    direction=mt5_direction, lot_size=_lot,
                                    stop_loss=report["stop"], take_profit=report["target"],
                                    strategy=STRATEGY_ORB_FIXED, source_name="ORB/IVB Report",
                                )
                                if ack.get("error"):
                                    raise RuntimeError(ack["error"])
                                result = ack.get("result") or {}
                            else:
                                result = await engine.open_manual_market_order(
                                    direction=mt5_direction, lot_size=_lot,
                                    stop_loss=report["stop"], take_profit=report["target"],
                                    strategy=STRATEGY_ORB_FIXED, source_name="ORB/IVB Report",
                                )
                            entry = float(result.get("entry_price", 0))
                            ticket = result.get("mt5_ticket", "—")
                            ui.notify(
                                f"{mt5_direction} opened @ {entry:.2f}  |  ticket {ticket}",
                                type="positive",
                            )
                        except Exception as exc:
                            status_lbl.text = str(exc)
                            ui.notify(str(exc), type="negative")
                        finally:
                            exec_btn.props("loading=false")
                            exec_btn.enable()

                    exec_btn = ui.button(
                        f"Execute {mt5_direction} at Market — ORB Setup",
                        on_click=_execute_orb,
                    ).classes("bg-amber-600 hover:bg-amber-500 text-white w-full py-3 text-base font-semibold")

                    with ui.row().classes("items-center gap-2 mt-2"):
                        ui.label("Lot size").classes("text-xs text-gray-400 font-medium")
                        _orb_lot_rs = trading_ctl.get_risk_settings()
                        lot_inp = ui.number(
                            value=float(_orb_lot_rs.get("orb_lot_size", 0) or 0),
                            min=0.0, max=10.0, step=0.01, format="%.2f",
                            placeholder="0 = auto-size from risk %",
                        ).classes("w-32")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Lots used for both the manual Execute button above and the "
                            "unattended auto-execute below. Saves automatically. "
                            "0 = auto-size from your Risk % and the stop distance."
                        )

                        def _lot_change(e):
                            trading_ctl.update_risk_settings({"orb_lot_size": float(e.value or 0)})

                        lot_inp.on_value_change(_lot_change)
                elif direction == "unconfirmed":
                    ui.label(
                        "Price broke the London opening range but is still inside the "
                        "Asian range — not confirmed. The manual Execute button appears "
                        "once price also clears the Asian range in the same direction."
                    ).classes("text-xs text-gray-500 italic mb-2")
                else:
                    ui.label(
                        "No breakout yet — the manual Execute button appears once price "
                        "clears both the London opening range and the Asian range."
                    ).classes("text-xs text-gray-500 italic mb-2")

                # Auto-execute toggle
                ui.separator().classes("my-3")
                rs_now = trading_ctl.get_risk_settings()
                auto_val = bool(rs_now.get("orb_auto_execute_enabled", 0))
                with ui.row().classes("items-center gap-2"):
                    auto_chk = ui.checkbox(
                        "Auto-execute this setup every morning (unattended)",
                        value=auto_val,
                    ).classes("text-sm text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When on, the app places this trade automatically at 08:15 UK time "
                        "each weekday using the recommendation above — no manual click needed. "
                        "In Remote mode, whichever node is the active trader executes it; the "
                        "other node stays silent to avoid a double trade. Off by default."
                    )

                    def _auto_toggle(e):
                        trading_ctl.update_risk_settings({"orb_auto_execute_enabled": 1 if e.value else 0})
                        ui.notify(
                            "ORB auto-execute enabled" if e.value else "ORB auto-execute disabled",
                            type="positive" if e.value else "info",
                        )

                    auto_chk.on_value_change(_auto_toggle)

    ui.timer(60.0, refresh)
async def _background_commentary(engine, signal_id: str):
    try:
        from backend.src.controllers import settings_controller as cfg_module
        from backend.src.controllers import ai_controller as claude_ai
        config = cfg_module.load_config()
        row = trading_ctl.get_signal(signal_id)
        tick    = await engine.get_tick()
        candles = await engine.get_candles("M5", 20)
        commentary = await claude_ai.request_commentary(
            "signal_saved", None, row, tick, candles, config,
        )
        if commentary.get("summary"):
            trading_ctl.set_signal_commentary(signal_id, commentary)
    except Exception as _exc:
        log.warning("Background commentary failed for signal %s: %s", signal_id, _exc)
