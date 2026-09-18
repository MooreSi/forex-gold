"""The Signal Decision Log readout (Parsing page).

docs/todo/signal-validation/010. The switch that turns the log on lives in
`_keywords.py` with the other parsing toggles; this is what it is for.

Its own module rather than a third `_render_*` in `_keywords.py`: that file
is the parsing toggles and the logic-keyword editor, and this is neither.

TWO READOUTS, BECAUSE THEY BECOME USEFUL AT DIFFERENT TIMES
-----------------------------------------------------------
**What happened** is available from the first decision: how many signals
each path executed and declined, and what did the declining. Nothing has to
close for it to mean something.

**Champion vs challenger** needs trades that have closed, so it says
nothing for days and is the reason the whole thing exists. `mean_r` comes
back as None rather than 0.0 for a variant that has scored nothing, and it
is rendered as "no decisions yet" -- printing 0.000 there would invite
somebody to act on evidence that does not exist.

Both are on a button rather than a timer. This sits under a live trading
page; a card that polls a database every few seconds to show a number that
moves twice a day is a cost with no benefit.
"""
from nicegui import ui

from backend.src.controllers import telegram_controller as tg_controller

__all__ = ["render_decision_log_section"]

_MUTED = "text-xs text-gray-400 font-mono"


def _render_summary(box: ui.column) -> None:
    box.clear()
    s = tg_controller.decision_log_summary()
    with box:
        if not s["total"]:
            ui.label(
                "Nothing recorded yet. Switch the log on above and it fills "
                "as signals arrive — or rebuild it from past trades below."
            ).classes("text-xs text-gray-500")
            return

        ui.label(
            f"{s['total']} decisions — {s['executed']} executed, "
            f"{s['blocked']} declined"
        ).classes("text-xs text-gray-300")
        ui.label(
            f"{s['resolved']} scored, {s['awaiting_outcome']} still open · "
            f"{s['observed']} observed, {s['reconstructed']} rebuilt from "
            f"past trades"
        ).classes(_MUTED)

        for row in s["by_path"]:
            name = "Immediate Market" if row["path"] == "ime" else "Full signal"
            ui.label(f"{name}: {row['executed']} executed, "
                     f"{row['blocked']} declined").classes(_MUTED)

        if s["top_reasons"]:
            ui.label("Why they were declined").classes(
                "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-2")
            for row in s["top_reasons"]:
                ui.label(f"{row['n']}x  {row['reason']}").classes(_MUTED)


def _render_report(box: ui.column) -> None:
    box.clear()
    rows = tg_controller.decision_log_report()
    with box:
        ui.label("Champion vs challenger").classes(
            "text-xs font-semibold text-gray-400 uppercase tracking-wider")
        for r in sorted(rows, key=lambda x: not x["is_champion"]):
            # None means the variant has scored nothing. Rendering it as
            # 0.000 would show no evidence and flat expectancy identically.
            mean_r = "no decisions yet" if r["mean_r"] is None \
                else f"{r['mean_r']:+.3f}R"
            abstained = f", {r['n_abstained']} no opinion" if r["n_abstained"] else ""
            label = r["variant"] + (" (live)" if r["is_champion"] else "")
            ui.label(f"{label}: took {r['n_taken']}, stood aside "
                     f"{r['n_skipped']}{abstained}, {mean_r}, "
                     f"net {r['net']:+.2f}").classes(_MUTED)


def render_decision_log_section() -> None:
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.icon("science", size="sm").classes("text-sky-400")
            ui.label("Signal Decision Log").classes(
                "text-base font-bold text-sky-300")
        ui.label(
            "What the app decided about each Telegram signal, and what four "
            "gates that are currently OFF would have decided. Recording only "
            "— nothing here changes a trade."
        ).classes("text-xs text-gray-500 mb-3")

        summary_box = ui.column().classes("w-full gap-0")
        report_box = ui.column().classes("w-full gap-0 mt-3")
        _render_summary(summary_box)

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Refresh", icon="refresh",
                      on_click=lambda: _render_summary(summary_box)) \
                .classes("text-xs bg-slate-800 text-white px-3").props("dense unelevated")
            ui.button("Champion vs challenger", icon="compare_arrows",
                      on_click=lambda: _render_report(report_box)) \
                .classes("text-xs bg-slate-800 text-white px-3").props("dense unelevated")

            def _backfill() -> None:
                added = tg_controller.decision_log_backfill()
                ui.notify(
                    f"Rebuilt {added} past decision(s)" if added
                    else "Nothing new to rebuild — every past trade is already in",
                    type="positive" if added else "info")
                _render_summary(summary_box)

            ui.button("Rebuild from past trades", icon="history",
                      on_click=_backfill) \
                .classes("text-xs bg-slate-700 text-white px-3").props("dense unelevated") \
                .tooltip(
                    "Reconstructs a decision for every past Telegram trade. "
                    "The clock gates and the entry trigger are rebuilt exactly; "
                    "the news calendar and the trend read are gone and record "
                    "no opinion. Rebuilt rows are marked as such. Safe to press "
                    "twice.")
