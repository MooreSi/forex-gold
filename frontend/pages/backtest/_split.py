"""The Backtest page's in-sample / out-of-sample summary -- docs/todo/003.

The Strategy Comparison table above this reports one number per strategy over
the whole loaded window, and the constants that number validates were chosen
by looking at that same window. This section reports the two halves
separately so the reader can see which part is the strategy and which part is
the choosing.

Two rules the rendering follows, both inherited from the "NOT SIMULATED"
precedent in `_results.py`:

- **A side with no number says why.** It never renders as zeros. A row of
  zeros beside a row with real figures reads as a strategy that traded
  nothing and lost nothing, which is an argument FOR the half that was never
  measured.
- **Out of sample is named as the line to believe**, unconditionally and in
  the section itself. The in-sample half was measured on the signals that
  chose the settings; leaving the reader to remember that is how a fitted
  number gets quoted as a result.
"""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from backend.src.controllers import backtest_controller as bt

# Same format and same clock as the page's "Date Ranges (UTC)" block. A
# signal's created_ts is true UTC; no broker offset belongs anywhere near it.
_TS_FMT = "%Y-%m-%d %H:%M UTC"

COLUMNS = ("Trades", "Win Rate", "Total P&L", "Prof Factor", "Max DD")


def utc_label(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_TS_FMT)


def side_row(side, note: str) -> list[str] | str:
    """One side's cells, or the note explaining why it has none.

    Pure, and public to this package's tests, because this is the
    decision -- the widget code below only renders whichever of the two it
    is handed. A thin side must never come back as a list of zeroed cells:
    that is the failure `unsupported_reason` already exists to prevent one
    table higher.
    """
    if side is None:
        return note
    return [f"{side.trades}", f"{side.win_rate:.1%}", f"${side.total_pnl:,.2f}",
            "∞" if side.profit_factor == float("inf") else f"{side.profit_factor:.2f}",
            f"{side.max_drawdown_pct:.1f}%"]


def _row(label: str, side, note: str, emphasis: bool) -> None:
    weight = "font-bold text-white" if emphasis else "text-gray-300"
    cells = side_row(side, note)
    with ui.element("tr").classes("border-b border-gray-700"):
        with ui.element("td").classes(f"px-3 py-2 whitespace-nowrap {weight}"):
            ui.label(label)
        if isinstance(cells, str):
            with ui.element("td").classes("px-3 py-2 text-gray-400 italic").props(
                    f'colspan={len(COLUMNS)}'):
                ui.label(cells)
            return
        for cell in cells:
            with ui.element("td").classes(f"px-3 py-2 text-center font-mono {weight}"):
                ui.label(cell)


def render_split(strategy_label: str, split) -> None:
    """One strategy's two halves. `split` is a bt.SplitStats."""
    with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
        ui.label(f"In sample vs out of sample — {strategy_label}").classes(
            "font-bold text-yellow-300")
        ui.label(
            f"Cut at {utc_label(split.boundary_ts)} · "
            f"{split.achieved_frac:.0%} of signals in sample"
            + ("" if abs(split.achieved_frac - split.requested_frac) < 1e-9
               else f" (asked for {split.requested_frac:.0%}; moved so signals "
                    "created at the same moment stay on one side)")
        ).classes("text-xs text-gray-400 mb-3")

        with ui.element("table").classes("w-full text-sm border-collapse"):
            with ui.element("thead"):
                with ui.element("tr").classes("border-b border-gray-600"):
                    for hdr in ("", *COLUMNS):
                        with ui.element("th").classes(
                                "text-gray-400 font-semibold px-3 py-2 whitespace-nowrap"):
                            ui.label(hdr)
            with ui.element("tbody"):
                _row("In sample", split.in_sample, split.in_sample_note, emphasis=False)
                _row("Out of sample", split.out_of_sample, split.out_of_sample_note,
                     emphasis=True)

        ui.label(
            "Out of sample is the line to believe. The in-sample half was measured "
            "on the signals that chose the settings, so part of its figure is the "
            f"choosing. A side with fewer than {bt.MIN_TRADES_PER_SIDE} trades reports "
            "its count instead of a rate — see docs/simon-handover/036."
        ).classes("text-xs text-gray-500 mt-3")
