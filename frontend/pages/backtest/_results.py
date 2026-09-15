"""The Backtest page's results: the strategy comparison table and trade list.

Moved verbatim out of the page module when it became a package
(docs/todo/003 phase 2). `_strategy_label`, `_OUTCOME_LABELS`, `_pnl_cls`,
`_fmt_pnl` and `_fmt_pf` came with it because this section is their only
caller -- they are internals of the results table, not shared page helpers.
"""
from __future__ import annotations

from typing import Optional

from nicegui import ui

from backend.src.controllers import backtest_controller as bt

TEMPLATE_PREFIX = "template:"


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

                        # A template this walk refused produces no trades, and
                        # a row of zeros reads as "traded nothing and lost
                        # nothing" -- which, beside a row showing a real
                        # drawdown, argues FOR the template that could not be
                        # simulated at all. Say refused instead of showing
                        # numbers that were never computed. (Owner, 2026-09-04:
                        # "why does the top strategy show all 0?" — a
                        # trail_mode=candle template on a tick run.)
                        if s.unsupported_reason:
                            with ui.element("tr").classes(
                                "border-b border-gray-700 bg-gray-800/40"
                            ):
                                with ui.element("td").classes("px-3 py-2"):
                                    ui.label(_strategy_label(strategy)).classes(
                                        "text-sm font-semibold text-gray-400"
                                    )
                                with ui.element("td").classes(
                                    "px-3 py-2 text-left"
                                ).props('colspan=12'):
                                    with ui.row().classes("items-center gap-2 flex-nowrap"):
                                        ui.badge("NOT SIMULATED", color="grey").classes(
                                            "text-xs shrink-0"
                                        )
                                        ui.label(s.unsupported_reason).classes(
                                            "text-xs text-orange-300"
                                        )
                            continue

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
