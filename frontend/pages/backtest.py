"""
Backtest page — simulate strategy performance against historical XAUUSD data.

Uses signals from vantage_signals DB or manually entered hypothetical signals,
fetches candle history from the MT5 bridge, and runs all selected strategies
side-by-side so you can compare which would have performed best.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Optional

from nicegui import ui

from backend.src.controllers import backtest_controller as bt

TEMPLATE_PREFIX = "template:"


def _template_choices() -> list[dict]:
    """Every EA template, with whether the backtest can simulate it.

    Read, never hardcoded -- the owner's requirement that "if new EA templates
    are created they should appear here automatically" is satisfied by asking
    the same store Trading > Strategy writes to.

    Unsupported templates are still listed, disabled, with the reason. Hiding
    them would read as "that template does not exist"; showing them greyed
    with "grid legs are not modelled" is the answer someone can act on.
    """
    from backend.src.controllers import broker_controller as _et

    try:
        return bt.summarise_templates(_et.list_ea_templates())
    except Exception:
        return []


def _strategy_label(key: str) -> str:
    """Display name for a results row's strategy key."""
    return key[len(TEMPLATE_PREFIX):] if key.startswith(TEMPLATE_PREFIX) else key

_OUTCOME_LABELS = {
    "sl":          ("SL", "text-red-400"),
    "tp1":         ("TP1", "text-green-300"),
    "tp1_only":    ("TP1+BE", "text-yellow-300"),
    "tp1_tp3":     ("TP1+TP3", "text-green-400"),
    "tp3_direct":  ("Full exit", "text-green-400"),
    "timeout":     ("Timeout", "text-gray-400"),
}


def _pnl_cls(v: float) -> str:
    return "text-green-400 font-mono" if v >= 0 else "text-red-400 font-mono"


def _fmt_pnl(v: float) -> str:
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _fmt_pf(v: float) -> str:
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


# ── Candle-per-day constants (XAUUSD: ~23 h/day Mon–Fri) ─────────────────────
_TF_INFO = {
    "M1":  ("M1",  1,  985,  14,
             "Best for single-signal accuracy — bars typically 1–4 pts, rarely spanning both SL and TP. "
             "Vantage depth ~2 weeks of M1 data; request a maximum of 14 days. "
             "Even M1 cannot capture per-second price spikes, but same-bar SL/TP ambiguity is minimal."),
    "M5":  ("M5",  5,  197,  90,
             "Recommended for scalping backtests. Bars typically 3–10 pts; the same range as most "
             "scalping SLs, so occasional same-bar ambiguity exists (simulation assumes TP1 hit first). "
             "Vantage depth ~3 months. Good balance of accuracy and history depth."),
    "M15": ("M15", 15, 66,   180,
             "Bars typically 8–25 pts — the same range as a scalping SL. When both SL and TP1 fall "
             "inside one bar the simulation assumes TP1 hit first (optimistic). "
             "Better for swing strategy context than tight scalp accuracy. Vantage depth ~6 months."),
    "M30": ("M30", 30, 33,   365,
             "Bars typically 15–40 pts — too coarse for scalping SL accuracy. "
             "Useful only for session-level or swing strategy analysis. Vantage depth ~12 months."),
}
# Fields: (label, tf_minutes, candles_per_calendar_day, max_days, description)


def _days_to_candles(tf: str, days: int) -> int:
    info = _TF_INFO.get(tf)
    if not info:
        return days * 197  # fallback to M5
    return max(100, round(days * info[2]))


def render(get_engine) -> None:
    engine = get_engine()

    # ── State ─────────────────────────────────────────────────────────────────
    manual_signals: list[bt.BtSignal] = []
    candles_cache: list[dict] = []
    results_container = None  # assigned below

    # ── Page header ───────────────────────────────────────────────────────────
    with ui.column().classes("w-full p-4 gap-4"):

        with ui.row().classes("items-center gap-2"):
            ui.icon("bar_chart").classes("text-yellow-300 text-2xl")
            ui.label("Backtest").classes("text-xl font-bold text-yellow-300")
        ui.label(
            "Simulate all loaded XAUUSD strategies against historical candle data. "
            "Load signals from the database or enter hypothetical signals manually, "
            "then compare strategies side-by-side."
        ).classes("text-gray-400 text-sm")

        # ── Configuration ─────────────────────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
            ui.label("Configuration").classes("font-bold text-yellow-300 mb-3")

            with ui.grid(columns=3).classes("w-full gap-4"):
                balance_inp = ui.number(
                    "Starting Balance ($)", value=1000, min=100, max=1_000_000, step=100
                ).classes("w-full")
                with ui.tooltip():
                    ui.label("Account balance at the start of the backtest period. Lot size updates dynamically as equity grows or falls.")

                risk_inp = ui.number(
                    "Risk per Trade (%)", value=1.0, min=0.1, max=5.0, step=0.1, format="%.1f"
                ).classes("w-full")
                with ui.tooltip():
                    ui.label("% of current equity risked per signal. Recalculated after every trade using the live balance, not the starting balance.")

                lots_inp = ui.number(
                    "Lots per Trade", value=0.0, min=0.0, max=5.0, step=0.01, format="%.2f"
                ).classes("w-full")
                with ui.tooltip():
                    ui.label(
                        "Fixed lot size for every trade. "
                        "If greater than zero this overrides the Risk per Trade % calculation. "
                        "Set to 0 to use Risk per Trade instead."
                    )

                spread_inp = ui.number(
                    "Spread (pts)", value=0.4, min=0.1, max=5.0, step=0.1, format="%.1f"
                ).classes("w-full")
                with ui.tooltip():
                    ui.label(
                        "Simulated bid/ask spread applied at fill. "
                        "Vantage Markets XAUUSD is typically 0.3–0.5 pts. "
                        "Shifts the actual fill price and therefore all strategy levels (SL, TP1, etc.)."
                    )

                commission_inp = ui.number(
                    "Commission ($/lot)", value=7.0, min=0.0, max=20.0, step=0.5, format="%.1f"
                ).classes("w-full")
                with ui.tooltip():
                    ui.label(
                        "Round-turn commission per standard lot deducted from every trade P&L. "
                        "Vantage Markets XAUUSD ECN account: ~$3/lot/side = $6–7/lot round trip. "
                        "Set to 0 to simulate raw price movement only."
                    )

                max_sl_inp = ui.number(
                    "Max SL Filter (pts)", value=50.0, min=5.0, max=200.0, step=5.0, format="%.0f"
                ).classes("w-full")
                with ui.tooltip():
                    ui.label(
                        "Skip signals whose SL is more than this many points from the entry midpoint. "
                        "Protects against corrupt DB entries (SL=0, parsing errors like SL=4 instead of 4244). "
                        "Also filters point-entry signals (entry_low == entry_high, no zone) and zero-SL instant orders."
                    )

            ui.label("EA Templates to compare").classes("text-blue-300 text-sm font-semibold mt-3 mb-1")
            strategy_checks: dict[str, ui.checkbox] = {}
            _choices = _template_choices()
            if not _choices:
                ui.label("No EA templates saved yet — create one under "
                         "Trading › Strategy.").classes("text-xs text-gray-500")
            with ui.row().classes("flex-wrap gap-3"):
                for _row in _choices:
                    _key = f"{TEMPLATE_PREFIX}{_row['name']}"
                    if _row["supported"]:
                        strategy_checks[_key] = ui.checkbox(_row["name"], value=False)
                    else:
                        # Disabled rather than hidden, with the reason on hover.
                        cb = ui.checkbox(_row["name"], value=False)
                        cb.props("disable").classes("opacity-50")
                        cb.tooltip("Not simulated — "
                                   + "; ".join(_row["reasons"]))

        # ── Signal source ──────────────────────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
            ui.label("Signal Source").classes("font-bold text-yellow-300 mb-3")

            source_radio = ui.radio(
                {
                    "db_all":   "All database signals (pending + triggered)",
                    "db_live":  "Live MT5 trades only (Breakout, Bounce, Telegram — executed signals)",
                    "manual":   "Manual Entry",
                },
                value="db_live",
            ).props("inline")

            # DB source info
            db_info_row = ui.row().classes("items-center gap-3 mt-2")
            with db_info_row:
                _all  = bt.signals_from_db(live_trades_only=False)
                _live = bt.signals_from_db(live_trades_only=True)
                db_count_lbl = ui.label(
                    f"{len(_live)} live MT5 trade signal(s)  /  {len(_all)} total in database"
                    + (f"  — oldest live trade: {time.strftime('%Y-%m-%d', time.gmtime(min(s.created_ts for s in _live)))}"
                       if _live else "")
                ).classes("text-gray-300 text-sm")
                if not _live:
                    ui.label(
                        "(No live trades yet — signals accumulate as Telegram/Breakout/Bounce send them)"
                    ).classes("text-orange-400 text-xs")

            # Manual entry form
            manual_form = ui.column().classes("w-full mt-2 gap-2").bind_visibility_from(
                source_radio, "value", lambda v: v == "manual"
            )
            manual_signals_list_el = None

            with manual_form:
                ui.label("Add a signal to test").classes("text-blue-300 text-sm font-semibold")

                with ui.grid(columns=4).classes("w-full gap-3"):
                    dir_sel        = ui.select({"BUY": "BUY", "SELL": "SELL"}, value="BUY", label="Direction").classes("w-full")
                    entry_low_inp  = ui.number("Entry Low",  value=3300.0, format="%.2f").classes("w-full")
                    entry_high_inp = ui.number("Entry High", value=3305.0, format="%.2f").classes("w-full")
                    sl_inp         = ui.number("Stop Loss",  value=3290.0, format="%.2f").classes("w-full")

                with ui.grid(columns=4).classes("w-full gap-3"):
                    tp1_inp = ui.number("TP1",           value=3315.0, format="%.2f").classes("w-full")
                    tp2_inp = ui.number("TP2 (optional)", value=0.0,  format="%.2f").classes("w-full")
                    tp3_inp = ui.number("TP3 (optional)", value=3330.0, format="%.2f").classes("w-full")
                    sig_ts  = ui.number("Signal timestamp (unix, 0=now)", value=0, format="%.0f").classes("w-full")

                sig_add_lbl = ui.label("").classes("text-sm text-gray-400")

                manual_signals_list_el = ui.column().classes("w-full gap-1 mt-1")
                _render_manual_list(manual_signals_list_el, manual_signals)

                def _add_signal():
                    try:
                        ts = float(sig_ts.value) or time.time()
                        tp1_val = float(tp1_inp.value) if tp1_inp.value else None
                        tp2_val = float(tp2_inp.value) if tp2_inp.value else None
                        tp3_val = float(tp3_inp.value) if tp3_inp.value else None
                        sig = bt.BtSignal(
                            signal_id  = str(uuid.uuid4())[:8],
                            direction  = dir_sel.value,
                            entry_low  = float(entry_low_inp.value),
                            entry_high = float(entry_high_inp.value),
                            stop_loss  = float(sl_inp.value),
                            tp1        = tp1_val,
                            tp2        = tp2_val if tp2_val else None,
                            tp3        = tp3_val,
                            created_ts = ts,
                            source     = "manual",
                        )
                        if sig.entry_low >= sig.entry_high:
                            sig_add_lbl.text = "Entry Low must be less than Entry High."
                            return
                        manual_signals.append(sig)
                        _render_manual_list(manual_signals_list_el, manual_signals)
                        sig_add_lbl.text = f"Added — {len(manual_signals)} signal(s) queued."
                        sig_add_lbl.classes(replace="text-sm text-green-400")
                    except Exception as e:
                        sig_add_lbl.text = f"Error: {e}"
                        sig_add_lbl.classes(replace="text-sm text-red-400")

                ui.button("Add Signal", icon="add", on_click=_add_signal).classes(
                    "bg-blue-700 text-white px-4 py-2"
                )

        # ── Candle data ────────────────────────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
            ui.label("Historical Price Data").classes("font-bold text-yellow-300 mb-3")

            with ui.row().classes("items-center gap-4 flex-wrap"):
                tf_sel = ui.select(
                    {k: v[0] for k, v in _TF_INFO.items()},
                    value="M5",
                    label="Timeframe",
                ).classes("w-32")

                days_inp = ui.number(
                    "Days", value=30, min=1, max=365, step=1, format="%.0f"
                ).classes("w-28")
                with ui.tooltip():
                    ui.label(
                        "Number of calendar days of history to fetch. "
                        "XAUUSD trades ~23 h/day Mon–Fri; candle count is auto-calculated from days × timeframe. "
                        "Vantage MT5 depth: M1 ~14 days, M5 ~3 months, M15 ~6 months, M30 ~12 months."
                    )

            candle_detail_lbl = ui.label("").classes("text-xs text-gray-400 mt-1 w-full font-mono")
            tf_advice_lbl     = ui.label("").classes("text-xs text-blue-300 mt-1 w-full")

            def _update_tf_info():
                tf        = tf_sel.value or "M5"
                days      = max(1, int(days_inp.value or 30))
                info      = _TF_INFO.get(tf, _TF_INFO["M5"])
                max_days  = info[3]
                n_candles = _days_to_candles(tf, days)

                # Warn if exceeding broker depth
                if days > max_days:
                    candle_detail_lbl.text = (
                        f"{days} days × ~{info[2]:,} candles/day ({tf}, ~23h/day Mon–Fri) "
                        f"= ~{n_candles:,} candles  —  WARNING: Vantage {tf} depth ~{max_days} days; "
                        f"older candles may not be available."
                    )
                    candle_detail_lbl.classes(replace="text-xs text-orange-400 mt-1 w-full font-mono")
                else:
                    candle_detail_lbl.text = (
                        f"{days} days × ~{info[2]:,} candles/day ({tf}, ~23h/day Mon–Fri) "
                        f"= ~{n_candles:,} candles  —  within Vantage {tf} depth ({max_days} days max)."
                    )
                    candle_detail_lbl.classes(replace="text-xs text-gray-400 mt-1 w-full font-mono")

                tf_advice_lbl.text = info[4]

            tf_sel.on_value_change(lambda _: _update_tf_info())
            days_inp.on_value_change(lambda _: _update_tf_info())
            _update_tf_info()

            with ui.row().classes("items-center gap-4 mt-3"):
                candle_status_lbl = ui.label("No candles loaded.").classes("text-gray-400 text-sm")
                fetch_btn = ui.button(
                    "Fetch Candles", icon="download",
                    on_click=lambda: asyncio.ensure_future(_fetch_candles())
                ).classes("bg-gray-700 text-white px-4 py-2")

        async def _fetch_candles():
            tf        = tf_sel.value or "M5"
            days      = max(1, int(days_inp.value or 30))
            n_candles = _days_to_candles(tf, days)
            candle_status_lbl.text = f"Fetching ~{n_candles:,} {tf} candles ({days} days)..."
            candle_status_lbl.classes(replace="text-gray-400 text-sm")
            fetch_btn.disable()
            try:
                raw = await engine.get_candles_for_symbol("XAUUSD", tf, n_candles)
                if not raw:
                    candle_status_lbl.text = "No candles returned — ensure the bridge is connected."
                    candle_status_lbl.classes(replace="text-orange-400 text-sm")
                    return
                candles_cache.clear()
                candles_cache.extend(raw)
                from datetime import datetime, timezone
                from backend.src.controllers.backtest_controller import BROKER_TZ_OFFSET as _BROKER_TZ_OFFSET
                # Candle ts is UTC+3; subtract offset for UTC display
                first_dt = datetime.fromtimestamp(raw[0]["ts"] - _BROKER_TZ_OFFSET, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                last_dt  = datetime.fromtimestamp(raw[-1]["ts"] - _BROKER_TZ_OFFSET, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                requested = n_candles
                received  = len(raw)
                note = "" if received >= requested * 0.9 else f"  (requested {requested:,}, got {received:,} — Vantage depth limit)"
                candle_status_lbl.text = (
                    f"{received:,} {tf} candles loaded  ({first_dt} → {last_dt} UTC){note}"
                )
                candle_status_lbl.classes(replace="text-green-400 text-sm" if not note else "text-yellow-400 text-sm")
            except Exception as e:
                candle_status_lbl.text = f"Fetch error: {e}"
                candle_status_lbl.classes(replace="text-red-400 text-sm")
            finally:
                fetch_btn.enable()

        # ── Run ────────────────────────────────────────────────────────────────
        run_status_lbl    = ui.label("").classes("text-sm text-gray-400")
        results_container = ui.column().classes("w-full gap-4")

        async def _run_backtest():
            selected = [k for k, cb in strategy_checks.items() if cb.value]
            if not selected:
                run_status_lbl.text = "Select at least one strategy."
                return
            if not candles_cache:
                run_status_lbl.text = "Fetch candles first."
                return

            src = source_radio.value
            if src == "db_all":
                raw_signals = bt.signals_from_db(live_trades_only=False)
                if not raw_signals:
                    run_status_lbl.text = "No signals in database. Use Manual Entry or wait for signals."
                    return
            elif src == "db_live":
                raw_signals = bt.signals_from_db(live_trades_only=True)
                if not raw_signals:
                    run_status_lbl.text = (
                        "No live MT5 trades in database yet. "
                        "Use 'All database signals' or Manual Entry."
                    )
                    return
            else:
                if not manual_signals:
                    run_status_lbl.text = "Add at least one signal in Manual Entry."
                    return
                raw_signals = manual_signals[:]

            # Filter signals: time window alignment + data quality validation
            max_sl = float(max_sl_inp.value or 50.0)
            signals, fstats = bt.filter_signals(raw_signals, candles_cache, max_sl_pts=max_sl)

            if not signals:
                run_status_lbl.text = (
                    f"0 valid signals after filtering (from {fstats.total} raw): "
                    f"{fstats.out_of_window} outside candle window, "
                    f"{fstats.zero_sl} no-SL, {fstats.point_entry} point-entry, "
                    f"{fstats.wide_sl} SL>{max_sl:.0f}pt, {fstats.bad_tp} bad TP. "
                    f"Try fetching more days, choosing 'All database signals', or reducing the Max SL Filter."
                )
                run_status_lbl.classes(replace="text-sm text-red-400")
                return

            lots      = float(lots_inp.value or 0.0)
            comm      = float(commission_inp.value or 0.0)
            lots_desc = f"fixed {lots:.2f} lots" if lots > 0 else f"{float(risk_inp.value or 1.0):.1f}% risk"
            run_status_lbl.text = (
                f"Running — {signals.__len__()} valid signal(s) of {fstats.total} raw "
                f"× {len(selected)} strategy/ies ({lots_desc}, ${comm:.1f}/lot commission)..."
            )
            run_status_lbl.classes(replace="text-sm text-yellow-300")
            results_container.clear()
            await asyncio.sleep(0)

            try:
                stats = bt.run_backtest(
                    signals             = signals,
                    candles             = candles_cache,
                    strategies          = selected,
                    starting_balance    = float(balance_inp.value or 1000),
                    risk_pct            = float(risk_inp.value or 1.0),
                    spread_pts          = float(spread_inp.value or 0.4),
                    lots_per_trade      = lots,
                    commission_per_lot  = comm,
                )
                run_status_lbl.text = (
                    f"Done — {fstats.valid} of {fstats.total} signals valid for this candle window "
                    f"({fstats.out_of_window} outside window, {fstats.zero_sl} no-SL, "
                    f"{fstats.point_entry} point-entry, {fstats.wide_sl} SL>{max_sl:.0f}pt, "
                    f"{fstats.bad_tp} bad TP)"
                )
                run_status_lbl.classes(replace="text-sm text-green-400")
                _render_results(results_container, stats, float(balance_inp.value or 1000), fstats)
            except Exception as e:
                run_status_lbl.text = f"Backtest error: {e}"
                run_status_lbl.classes(replace="text-sm text-red-400")

        ui.button(
            "Run Backtest", icon="play_arrow", on_click=lambda: asyncio.ensure_future(_run_backtest())
        ).classes("bg-green-700 hover:bg-green-600 text-white font-bold px-6 py-2 text-base")

        results_container


def _render_manual_list(container: ui.column, signals: list[bt.BtSignal]) -> None:
    container.clear()
    if not signals:
        with container:
            ui.label("No signals added yet.").classes("text-gray-500 text-xs")
        return
    with container:
        for i, s in enumerate(signals):
            with ui.row().classes("items-center gap-2 text-xs text-gray-300"):
                dir_cls = "text-green-400" if s.direction == "BUY" else "text-red-400"
                ui.label(f"#{i + 1}").classes("text-gray-500 w-6")
                ui.label(s.direction).classes(f"font-bold {dir_cls} w-10")
                ui.label(f"Zone {s.entry_low:.2f}–{s.entry_high:.2f}").classes("font-mono w-36")
                ui.label(f"SL {s.stop_loss:.2f}").classes("font-mono w-24")
                ui.label(f"TP1 {s.tp1:.2f}" if s.tp1 else "TP1 —").classes("font-mono w-24")
                ui.label(f"TP2 {s.tp2:.2f}" if s.tp2 else "TP2 —").classes("font-mono w-24")
                ui.label(f"TP3 {s.tp3:.2f}" if s.tp3 else "TP3 —").classes("font-mono w-24")
                ui.label(f"src={s.source}").classes("text-gray-500")


def _render_results(
    container: ui.column,
    stats: dict[str, bt.StrategyStats],
    starting_balance: float,
    fstats: Optional[bt.FilterStats] = None,
) -> None:
    with container:

        # ── Signal filter report ──────────────────────────────────────────────
        if fstats and fstats.total > 0:
            with ui.card().classes("w-full bg-gray-900 rounded-lg p-3"):
                with ui.row().classes("items-start gap-6 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        ui.label("Signal Quality Filter").classes("text-xs font-bold text-blue-300")
                        ui.label(f"Total raw signals: {fstats.total}").classes("text-xs text-gray-300 font-mono")
                        ui.label(f"Valid (tested):    {fstats.valid}").classes("text-xs text-green-400 font-mono font-bold")
                    with ui.column().classes("gap-1"):
                        ui.label("Rejected").classes("text-xs font-bold text-orange-300")
                        if fstats.out_of_window:
                            ui.label(f"  Outside candle window: {fstats.out_of_window}").classes("text-xs text-orange-400 font-mono")
                        if fstats.zero_sl:
                            ui.label(f"  No stop loss (SL=0):   {fstats.zero_sl}").classes("text-xs text-orange-400 font-mono")
                        if fstats.point_entry:
                            ui.label(f"  Point entry (no zone): {fstats.point_entry}").classes("text-xs text-orange-400 font-mono")
                        if fstats.wide_sl:
                            ui.label(f"  Excessive SL distance: {fstats.wide_sl}").classes("text-xs text-orange-400 font-mono")
                        if fstats.bad_tp:
                            ui.label(f"  TP on wrong side:      {fstats.bad_tp}").classes("text-xs text-orange-400 font-mono")
                        if not any([fstats.out_of_window, fstats.zero_sl, fstats.point_entry, fstats.wide_sl, fstats.bad_tp]):
                            ui.label("  None — all signals passed").classes("text-xs text-green-400 font-mono")
                    with ui.column().classes("gap-1"):
                        ui.label("Date Ranges (UTC)").classes("text-xs font-bold text-gray-400")
                        if fstats.candle_start:
                            ui.label(f"  Candles: {fstats.candle_start}").classes("text-xs text-gray-300 font-mono")
                            ui.label(f"        → {fstats.candle_end}").classes("text-xs text-gray-300 font-mono")
                        if fstats.signal_start:
                            ui.label(f"  Signals: {fstats.signal_start}").classes("text-xs text-blue-300 font-mono")
                            ui.label(f"        → {fstats.signal_end}").classes("text-xs text-blue-300 font-mono")

        # ── Summary comparison table ──────────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
            ui.label("Strategy Comparison").classes("font-bold text-yellow-300 mb-3")

            with ui.element("table").classes("w-full text-sm border-collapse"):
                with ui.element("thead"):
                    with ui.element("tr").classes("border-b border-gray-600"):
                        for hdr, tip in [
                            ("Strategy",    "Strategy name"),
                            ("Trades",      "Signals where price entered the entry zone"),
                            ("Wins",        "Trades closed with positive P&L"),
                            ("Losses",      "Trades closed with negative P&L"),
                            ("Win Rate",    "Wins ÷ total filled trades"),
                            ("Total P&L",   "Sum of all trade P&L in USD (after commission)"),
                            ("Commission",  "Total commission paid (round-turn, all trades)"),
                            ("Avg Win",     "Average USD profit on winning trades"),
                            ("Avg Loss",    "Average USD loss on losing trades"),
                            ("Prof Factor", "Gross wins ÷ gross losses"),
                            ("Max DD",      "Maximum peak-to-trough drawdown %"),
                            ("Sharpe",      "Risk-adjusted return (avg P&L ÷ std deviation)"),
                            ("Final Bal",   "Starting balance + total P&L"),
                        ]:
                            with ui.element("th").classes(
                                "text-left text-gray-400 font-semibold px-3 py-2 whitespace-nowrap"
                            ):
                                ui.label(hdr)
                                if tip:
                                    with ui.tooltip():
                                        ui.label(tip)

                with ui.element("tbody"):
                    best_pnl = max((s.total_pnl for s in stats.values()), default=0)
                    for strategy, s in stats.items():
                        row_cls = "border-b border-gray-700 hover:bg-gray-750"
                        is_best = s.total_pnl == best_pnl and best_pnl > 0 and len(stats) > 1
                        if is_best:
                            row_cls += " bg-green-900/20"

                        with ui.element("tr").classes(row_cls):
                            with ui.element("td").classes("px-3 py-2"):
                                lbl = _strategy_label(strategy)
                                ui.label(lbl + (" ★" if is_best else "")).classes(
                                    "text-sm font-semibold "
                                    + ("text-green-300" if is_best else "text-gray-200")
                                )

                            with ui.element("td").classes("px-3 py-2 text-center font-mono text-gray-300"):
                                ui.label(str(s.trades))
                            with ui.element("td").classes("px-3 py-2 text-center font-mono text-green-400"):
                                ui.label(str(s.wins))
                            with ui.element("td").classes("px-3 py-2 text-center font-mono text-red-400"):
                                ui.label(str(s.losses))

                            with ui.element("td").classes("px-3 py-2 text-center font-mono"):
                                pct = s.win_rate * 100
                                ui.label(f"{pct:.0f}%").classes(
                                    "text-green-400" if pct >= 50 else "text-red-400"
                                )

                            with ui.element("td").classes(f"px-3 py-2 text-center {_pnl_cls(s.total_pnl)}"):
                                ui.label(_fmt_pnl(s.total_pnl))
                            with ui.element("td").classes("px-3 py-2 text-center text-orange-400 font-mono"):
                                ui.label(f"-${s.total_commission:,.2f}")
                            with ui.element("td").classes("px-3 py-2 text-center text-green-400 font-mono"):
                                ui.label(f"${s.avg_win:,.2f}")
                            with ui.element("td").classes("px-3 py-2 text-center text-red-400 font-mono"):
                                ui.label(f"${abs(s.avg_loss):,.2f}")

                            with ui.element("td").classes("px-3 py-2 text-center font-mono"):
                                pf_cls = ("text-green-400" if s.profit_factor >= 1.5
                                          else ("text-yellow-400" if s.profit_factor >= 1.0 else "text-red-400"))
                                ui.label(_fmt_pf(s.profit_factor)).classes(pf_cls)

                            with ui.element("td").classes("px-3 py-2 text-center font-mono"):
                                dd_cls = ("text-red-400" if s.max_drawdown_pct > 10
                                          else ("text-yellow-400" if s.max_drawdown_pct > 5 else "text-green-400"))
                                ui.label(f"{s.max_drawdown_pct:.1f}%").classes(dd_cls)

                            with ui.element("td").classes("px-3 py-2 text-center font-mono text-gray-300"):
                                ui.label(f"{s.sharpe:.2f}")
                            with ui.element("td").classes(f"px-3 py-2 text-center {_pnl_cls(s.total_pnl)} font-bold"):
                                ui.label(f"${s.final_balance:,.2f}")

        # ── Per-strategy trade logs ───────────────────────────────────────────
        for strategy, s in stats.items():
            if not s.trade_list:
                continue
            label = _strategy_label(strategy)
            with ui.expansion(
                f"{label}  —  {s.trades} trade(s), {_fmt_pnl(s.total_pnl)}",
                icon="list",
            ).classes("w-full bg-gray-800 rounded-lg"):
                with ui.element("table").classes("w-full text-xs border-collapse mt-2"):
                    with ui.element("thead"):
                        with ui.element("tr").classes("border-b border-gray-700"):
                            for h in ["#", "Dir", "Fill", "Close", "Lots", "Outcome", "Hold (bars)", "P&L pts", "Comm $", "P&L $"]:
                                with ui.element("th").classes(
                                    "text-left text-gray-400 px-2 py-1 whitespace-nowrap"
                                ):
                                    ui.label(h)
                    with ui.element("tbody"):
                        for i, t in enumerate(s.trade_list):
                            outcome_label, outcome_cls = _OUTCOME_LABELS.get(
                                t.outcome, (t.outcome, "text-gray-400")
                            )
                            dir_cls = "text-green-400" if t.direction == "BUY" else "text-red-400"
                            with ui.element("tr").classes("border-b border-gray-700/50 hover:bg-gray-700/30"):
                                with ui.element("td").classes("px-2 py-1 text-gray-500"):
                                    ui.label(str(i + 1))
                                with ui.element("td").classes(f"px-2 py-1 font-bold {dir_cls}"):
                                    ui.label(t.direction)
                                with ui.element("td").classes("px-2 py-1 font-mono text-gray-300"):
                                    ui.label(f"{t.fill_price:.2f}")
                                with ui.element("td").classes("px-2 py-1 font-mono text-gray-300"):
                                    ui.label(f"{t.close_price:.2f}")
                                with ui.element("td").classes("px-2 py-1 font-mono text-gray-400"):
                                    ui.label(f"{t.lot_size:.2f}")
                                with ui.element("td").classes(f"px-2 py-1 font-semibold {outcome_cls}"):
                                    ui.label(outcome_label)
                                with ui.element("td").classes("px-2 py-1 font-mono text-gray-400 text-center"):
                                    ui.label(str(t.hold_bars))
                                with ui.element("td").classes(f"px-2 py-1 {_pnl_cls(t.pnl_pts)}"):
                                    ui.label(f"{t.pnl_pts:+.2f}")
                                with ui.element("td").classes("px-2 py-1 font-mono text-orange-400"):
                                    ui.label(f"-${t.commission:.2f}" if t.commission else "—")
                                with ui.element("td").classes(f"px-2 py-1 font-bold {_pnl_cls(t.pnl_usd)}"):
                                    ui.label(_fmt_pnl(t.pnl_usd))
