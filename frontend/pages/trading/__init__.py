"""Trading page — active positions, signal entry, strategy, Telegram signals.

Split into sections (M2). This module keeps `render()` and the page's own
wiring; each section owns one part of the page:

    _active_trades    open positions, including remote-node trades
    _pending_signals  signals waiting to activate
    _manual_entry     signal form, market order form, ORB/IVB report
    _schedule         trading schedule + strategy comparison
    _strategy         channel overrides, parameters, EA templates
    _signals_card     the signals card (the Telegram page renders it too)
    _tg_signals       raw Telegram signal list
    _shared           formatting helpers used by more than one section

The public surface is unchanged: render, render_signals_card,
trade_source_label and trade_channel_label all still import from
`frontend.pages.trading`.
"""

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)

from nicegui import ui

from backend.src.controllers import trading_controller as trading_ctl



# trade_source_label / trade_channel_label moved to the history controller
# (M3 page drain) -- they are display shaping the controllers need too.
# Re-imported here because several pages import them from this module.
from backend.src.controllers.history_controller import (  # noqa: E402,F401
    trade_channel_label, trade_source_label,
)






def render(get_engine: Callable, get_tg_reader: Callable):
    engine = get_engine()

    # ── Account summary row (live from MT5) ───────────────────────────────────
    with ui.row().classes("w-full gap-6 px-4 pt-3 pb-1 flex-wrap items-center"):
        balance_lbl = ui.label("Balance: $—").classes("text-base font-bold text-yellow-300")
        equity_lbl  = ui.label("Equity: $—").classes("text-base font-bold text-green-300")
        pnl_lbl     = ui.label("Net P&L (Total): $—").classes("text-base font-bold")
        pnl_lbl.tooltip(
            "Current equity minus total deposits since account inception — lifetime P&L. "
            "Spread cost is already reflected here since it's embedded in MT5's real fill "
            "prices; this account has 0% commission (Vantage Standard STP), so no further "
            "cost deduction applies."
        )
        open_lbl    = ui.label("Open: 0").classes("text-sm text-gray-400")
        wr_lbl      = ui.label("Win rate: —%").classes("text-sm text-gray-400")
        src_lbl     = ui.label("").classes("text-xs text-gray-600")
        cb_lbl      = ui.label("").classes("ml-auto text-sm font-bold text-red-400").set_visibility(False)

    async def _refresh_account():
        try:
            perf = await engine.compute_mt5_performance(90)
            if perf:
                balance_lbl.text = f"Balance: ${perf['balance']:,.2f}"
                equity_lbl.text  = f"Equity: ${perf['equity']:,.2f}"
                deposits         = await engine.get_total_deposits()
                lifetime_pnl     = perf["equity"] - deposits
                pnl_lbl.text     = f"Net P&L (Total): ${lifetime_pnl:+.2f}"
                pnl_lbl.classes(replace=f"text-base font-bold {_pnl_colour(lifetime_pnl)}")
                open_lbl.text    = (
                    f"Open: {perf['open_trades']}  Closed: {perf['closed_trades']}"
                )
                wr_lbl.text      = (
                    f"Win rate: {perf['win_rate_pct']:.1f}%  "
                    f"PF: {perf['profit_factor']:.2f}"
                )
                src_lbl.text = "MT5"
            else:
                # fallback to local DB
                local = engine.compute_performance()
                balance_lbl.text = f"Balance: ${local['current_balance']:,.2f}"
                equity_lbl.text  = f"Equity: ${local['equity']:,.2f}"
                pnl_lbl.text     = f"Net P&L (Total): ${local['total_net_pnl']:+.2f}"
                pnl_lbl.classes(replace=f"text-base font-bold {_pnl_colour(local['total_net_pnl'])}")
                open_lbl.text    = f"Open: {local['open_trades']}  Closed: {local['closed_trades']}"
                wr_lbl.text      = f"Win rate: {local['win_rate_pct']:.1f}%  PF: {local['profit_factor']:.2f}"
                src_lbl.text     = "local"
        except Exception as e:
            log.debug("[trading] account summary refresh failed: %s", e)
        # Circuit breaker badge (right-aligned)
        try:
            _cb = trading_ctl.get_circuit_breaker_state()
            if _cb["is_active"]:
                _rem = int(_cb["remaining_secs"])
                _hms = f"{_rem // 3600:02d}:{(_rem % 3600) // 60:02d}:{_rem % 60:02d}"
                cb_lbl.text = f"CIRCUIT BREAKER ACTIVE — resumes in {_hms}"
                cb_lbl.set_visibility(True)
            else:
                cb_lbl.set_visibility(False)
        except Exception as e:
            log.debug("[trading] circuit-breaker banner refresh failed: %s", e)

    ui.timer(5.0, _refresh_account)
    asyncio.ensure_future(_refresh_account())

    ui.separator().classes("my-1")

    # ── Sub-tabs ───────────────────────────────────────────────────────────────
    with ui.tabs().classes("bg-gray-800") as trade_tabs:
        t_strategy = ui.tab("Strategy")
        t_active   = ui.tab("Active Trades")
        t_pending  = ui.tab("Pending Signals")
        t_signal   = ui.tab("Limit Order")
        t_market   = ui.tab("Market Order")
        t_tg_sigs  = ui.tab("TG Signals")
        t_orb      = ui.tab("ORB/IVB Report")
        t_schedule = ui.tab("Schedule")

    with ui.tab_panels(trade_tabs, value=t_strategy).classes("bg-gray-900 p-4"):

        with ui.tab_panel(t_strategy):
            _render_strategy(engine)

        with ui.tab_panel(t_active):
            _render_active_trades(engine)

        with ui.tab_panel(t_pending):
            _render_pending_signals(engine)

        with ui.tab_panel(t_signal):
            _render_signal_entry(engine)

        with ui.tab_panel(t_market):
            _render_market_order_form(engine)

        with ui.tab_panel(t_tg_sigs):
            _render_tg_signals(engine)

        with ui.tab_panel(t_orb):
            _render_orb_report(engine)

        with ui.tab_panel(t_schedule):
            _render_schedule()


# ── Helpers ────────────────────────────────────────────────────────────────────







# ── Active trades ──────────────────────────────────────────────────────────────



# ── Pending signals ────────────────────────────────────────────────────────────



# ── Manual signal entry ────────────────────────────────────────────────────────









# ── Strategy comparison data ───────────────────────────────────────────────────

from backend.src.controllers.trading_controller import (
    STRATEGY_SCALE_OUT as _SO,
    STRATEGY_BE_RUNNER as _BE,
    STRATEGY_TRAIL_STOP as _TS,
    STRATEGY_PROTECTED_SCALE as _PS,
    STRATEGY_CONSERVATIVE as _CO,
    STRATEGY_NO_SL_SCALE as _NSS,
    STRATEGY_CONSERVATIVE_TRIAL as _CT,
    STRATEGY_SCALP_RUNNER as _SR,
    STRATEGY_SIGNAL_CLIMBER as _SC,
    STRATEGY_REVERSAL_RUNNER as _RVR,
    STRATEGY_ADAPTIVE_RUNNER as _AR,
    STRATEGY_ADAPTIVE_RUNNER_2 as _AR2,
)

# Sibling sections of this page.
from ._active_trades import _render_active_trades
from ._manual_entry import (
    _render_market_order_form,
    _render_orb_report,
    _render_signal_entry,
)
from ._pending_signals import _render_pending_signals
from ._schedule import _render_schedule
from ._shared import _pnl_colour
from ._strategy import _render_strategy
from ._tg_signals import _render_tg_signals
from ._signals_card import render_signals_card

















_STRAT_ORDER = [_SO, _BE, _TS, _PS, _CO, _NSS, _CT, _SR, _SC, _RVR, _AR, _AR2]










# ── Strategy ───────────────────────────────────────────────────────────────────















# ── TG Signals ─────────────────────────────────────────────────────────────────


# ── Public surface ────────────────────────────────────────────────────────────
# Re-exported so callers keep importing them from this package, not from the
# private section modules: telegram.py renders the signals card, and chart.py
# uses the label helpers.
__all__ = [
    "render",
    "render_signals_card",
    "trade_source_label",
    "trade_channel_label",
]
