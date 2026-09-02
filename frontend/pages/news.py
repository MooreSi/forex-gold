"""
News tab — economic calendar for the current week, ranked by relevance to gold,
plus the controls for the high-impact trading blackout.

Data comes from core/news_calendar.py (ForexFactory weekly feed). The feed only
publishes the current Mon-Sun window, so the horizon here is whatever is left of
this week.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from nicegui import ui

from frontend.components.poll import poll

from backend.src.controllers import settings_controller as cfg_module
from backend.src.controllers import news_controller as nc

# Impact -> (chip colour, text colour, label)
_IMPACT_STYLE: dict[str, tuple[str, str, str]] = {
    "high":    ("bg-red-600",    "text-red-200",    "HIGH"),
    "medium":  ("bg-amber-600",  "text-amber-200",  "MED"),
    "low":     ("bg-gray-600",   "text-gray-300",   "LOW"),
    "holiday": ("bg-blue-700",   "text-blue-200",   "HOL"),
}

_FILTERS: dict[str, set[str]] = {
    "High only":   {"high"},
    "High + Med":  {"high", "medium"},
    "All":         {"high", "medium", "low", "holiday"},
}


def _fmt_countdown(seconds: float) -> str:
    """Signed seconds -> '2h 14m' / '18m' / 'now'."""
    mins = int(abs(seconds) // 60)
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


def render() -> None:
    _filter_name: list[str] = ["High + Med"]
    _gold_only:   list[bool] = [True]

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1 flex-wrap gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("newspaper").classes("material-icons text-yellow-400 text-xl")
            ui.label("Economic Calendar").classes("text-lg font-bold text-yellow-300")
            ui.badge("XAUUSD Gold", color="amber").classes("text-xs")
        with ui.row().classes("items-center gap-3"):
            source_lbl = ui.label("").classes("text-xs text-gray-500")
            ui.button("Refresh", icon="refresh", on_click=lambda: _refresh(force=True)) \
                .classes("bg-gray-700 text-white text-sm px-3 py-1")

    ui.separator().classes("my-1")

    with ui.column().classes("w-full px-4 pb-4 gap-3"):
        # ── Next event / current blackout banner ──────────────────────────────
        banner = ui.card().classes("w-full rounded-lg p-3")

        # ── Blackout controls ─────────────────────────────────────────────────
        settings = nc.get_blackout_settings()
        with ui.card().classes("w-full bg-gray-800 border border-blue-600 p-4 rounded-lg gap-2"):
            with ui.row().classes("w-full items-center justify-between"):
                blackout_sw = ui.switch(
                    "Blackout trading around news",
                    value=settings["enabled"],
                ).classes("text-blue-300 font-bold")
                ui.icon("block", size="sm").classes("text-blue-400")

            with ui.row().classes("items-center gap-4 flex-wrap"):
                impact_sel = ui.select(
                    {"high": "High impact only", "high_medium": "High + Medium impact"},
                    value=settings["impact"],
                    label="Blackout on",
                ).classes("w-56 text-sm")
                before_in = ui.number(
                    label="Minutes before", value=settings["minutes_before"],
                    min=0, max=240, step=5, format="%d",
                ).classes("w-36 text-sm")
                after_in = ui.number(
                    label="Minutes after", value=settings["minutes_after"],
                    min=0, max=240, step=5, format="%d",
                ).classes("w-36 text-sm")
                save_btn = ui.button("Save", icon="save") \
                    .classes("bg-blue-600 text-white text-sm px-4 py-1 self-end")

            ui.label(
                "Applies to every automated entry: Signal Generator, Breakout, "
                "Reversal Engine, Telegram-copied signals and Instant Market Entry. "
                "Open positions are never closed or modified, manual market orders "
                "are not affected, and a pending order placed earlier can still fill "
                "inside a window."
            ).classes("text-xs text-gray-500")

        def _save_settings():
            cfg_module.save_config({
                "news_blackout_enabled":        bool(blackout_sw.value),
                "news_blackout_impact":         str(impact_sel.value),
                "news_blackout_minutes_before": int(before_in.value or 0),
                "news_blackout_minutes_after":  int(after_in.value or 0),
            })
            ui.notify("News blackout settings saved", type="positive")
            _refresh()

        save_btn.on_click(_save_settings)

        # ── Filters ───────────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.label("Show:").classes("text-xs text-gray-500")
            impact_toggle = ui.toggle(
                list(_FILTERS), value=_filter_name[0],
            ).props("dense no-caps").classes("text-xs")
            gold_sw = ui.switch("Gold-relevant currencies only", value=_gold_only[0]) \
                .classes("text-xs text-gray-400")

        def _on_filter(_=None):
            _filter_name[0] = impact_toggle.value or "High + Med"
            _gold_only[0]   = bool(gold_sw.value)
            _refresh()

        impact_toggle.on_value_change(_on_filter)
        gold_sw.on_value_change(_on_filter)

        # ── Event list ────────────────────────────────────────────────────────
        events_area = ui.column().classes("w-full gap-3")

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_banner():
        banner.clear()
        current = nc.get_current_event()
        upcoming = nc.get_events(
            impacts={"high"}, currencies={"USD", "XAU"}, upcoming_only=True,
        )
        with banner:
            if current:
                banner.classes(replace="w-full rounded-lg p-3 bg-red-900 border border-red-500")
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    ui.icon("block", size="sm").classes("text-red-300")
                    ui.label("BLACKOUT ACTIVE").classes("text-sm font-bold text-red-200")
                    ui.label(f"{current['title']} ({current['currency']})") \
                        .classes("text-sm text-red-100")
                    ui.label(f"resumes in {_fmt_countdown(current['mins_remaining'] * 60)}") \
                        .classes("text-xs text-red-300")
            elif upcoming:
                # Several events routinely share a slot — NFP, Unemployment Rate
                # and Average Hourly Earnings all print at 12:30 UTC. Name the
                # one that actually moves gold, not whichever sorted first.
                soonest_ts = upcoming[0]["ts"]
                nxt = max(
                    (e for e in upcoming if e["ts"] == soonest_ts),
                    key=lambda e: e["score"],
                )
                delta = nxt["ts"] - time.time()
                banner.classes(replace="w-full rounded-lg p-3 bg-gray-800 border border-gray-700")
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    ui.icon("schedule", size="sm").classes("text-yellow-400")
                    ui.label("Next high impact").classes("text-sm font-bold text-yellow-300")
                    ui.label(f"{nxt['title']} ({nxt['currency']})") \
                        .classes("text-sm text-gray-200")
                    ui.label(f"in {_fmt_countdown(delta)}").classes("text-xs text-gray-400")
                    ui.label(f"{nxt['dt']:%a %d %b %H:%M} UTC").classes("text-xs text-gray-500")
            elif not nc.get_events():
                # No events at all means the feed is unreachable and nothing was
                # ever cached, not a quiet week. Saying "all clear" here would
                # repeat the silent-failure shape this whole feature came from.
                banner.classes(replace="w-full rounded-lg p-3 bg-amber-900 border border-amber-600")
                with ui.row().classes("items-center gap-3 flex-wrap"):
                    ui.icon("warning", size="sm").classes("text-amber-300")
                    ui.label("Calendar unavailable").classes("text-sm font-bold text-amber-200")
                    ui.label(
                        "The ForexFactory feed could not be reached. The blackout is "
                        "falling back to a fixed schedule of the routine gold movers."
                        if nc.get_blackout_settings()["enabled"] else
                        "The ForexFactory feed could not be reached."
                    ).classes("text-xs text-amber-100")
            else:
                banner.classes(replace="w-full rounded-lg p-3 bg-gray-800 border border-gray-700")
                with ui.row().classes("items-center gap-3"):
                    ui.icon("check_circle", size="sm").classes("text-green-400")
                    ui.label("No further high-impact gold events this week") \
                        .classes("text-sm text-gray-300")

    def _render_events():
        events_area.clear()
        impacts = _FILTERS[_filter_name[0]]
        currencies = {"USD", "XAU", "EUR", "GBP", "CNY"} if _gold_only[0] else None
        events = nc.get_events(impacts=impacts, currencies=currencies)

        with events_area:
            if not events:
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-8 text-center"):
                    ui.label("event_busy").classes("material-icons text-5xl text-gray-600 mb-2")
                    if nc.get_events():
                        ui.label("No events match the current filter") \
                            .classes("text-gray-400 text-sm")
                        ui.label(
                            "The feed publishes the current week only — later in the "
                            "week there is naturally less left to show."
                        ).classes("text-gray-600 text-xs mt-1")
                    else:
                        ui.label("No calendar data").classes("text-gray-400 text-sm")
                        ui.label(
                            "The feed could not be reached. Retrying automatically; "
                            "Refresh forces an immediate attempt."
                        ).classes("text-gray-600 text-xs mt-1")
                return

            now_ts = time.time()
            local_tz = datetime.now().astimezone().tzinfo

            # Group by UTC day, keeping chronological order within each day.
            by_day: dict[str, list[dict]] = {}
            for ev in events:
                by_day.setdefault(f"{ev['dt']:%A %d %B}", []).append(ev)

            for day, day_events in by_day.items():
                is_today = day_events[0]["dt"].date() == datetime.now(timezone.utc).date()
                with ui.card().classes("w-full bg-gray-800 rounded-lg p-0 overflow-hidden"):
                    with ui.row().classes(
                        "w-full items-center gap-2 px-3 py-2 "
                        + ("bg-yellow-900" if is_today else "bg-gray-700")
                    ):
                        ui.label(day).classes(
                            "text-sm font-bold "
                            + ("text-yellow-200" if is_today else "text-gray-300")
                        )
                        if is_today:
                            ui.badge("TODAY", color="amber").classes("text-xs")
                        n = len(day_events)
                        ui.label(f"{n} event" + ("" if n == 1 else "s")).classes(
                            "text-xs text-gray-400 ml-auto"
                        )

                    for ev in day_events:
                        passed = ev["ts"] < now_ts
                        bg, txt, label = _IMPACT_STYLE.get(
                            ev["impact"], _IMPACT_STYLE["low"]
                        )
                        row_cls = "w-full items-center gap-3 px-3 py-2 border-t border-gray-700"
                        if passed:
                            row_cls += " opacity-50"
                        with ui.row().classes(row_cls):
                            ui.label(label).classes(
                                f"{bg} text-white text-xs font-bold px-2 py-0.5 "
                                "rounded shrink-0 w-12 text-center"
                            )
                            with ui.column().classes("gap-0 shrink-0 w-28"):
                                ui.label(f"{ev['dt']:%H:%M} UTC").classes(
                                    "text-xs font-mono text-gray-300 leading-tight"
                                )
                                ui.label(
                                    f"{ev['dt'].astimezone(local_tz):%H:%M} local"
                                ).classes("text-xs font-mono text-gray-500 leading-tight")
                            ui.label(ev["currency"]).classes(
                                "text-xs font-bold text-gray-400 shrink-0 w-10"
                            )
                            ui.label(ev["title"]).classes(f"text-sm {txt} flex-1 min-w-0")

                            if ev["forecast"] or ev["previous"]:
                                with ui.column().classes("gap-0 shrink-0 w-28 text-right"):
                                    if ev["forecast"]:
                                        ui.label(f"fc {ev['forecast']}").classes(
                                            "text-xs font-mono text-gray-400 leading-tight"
                                        )
                                    if ev["previous"]:
                                        ui.label(f"prev {ev['previous']}").classes(
                                            "text-xs font-mono text-gray-600 leading-tight"
                                        )

                            ui.label(
                                "passed" if passed
                                else f"in {_fmt_countdown(ev['ts'] - now_ts)}"
                            ).classes("text-xs text-gray-500 shrink-0 w-20 text-right")
                            ui.label(f"{ev['score']:.1f}").tooltip(
                                "Gold relevance: impact rating weighted by currency, "
                                "boosted for events that historically move gold hardest"
                            ).classes(
                                "text-xs font-mono shrink-0 w-8 text-right "
                                + ("text-yellow-400" if ev["score"] >= 2.5 else "text-gray-600")
                            )

    def _refresh(force: bool = False):
        if force:
            nc.invalidate_cache()
        total = len(nc.get_events())
        source_lbl.text = (
            f"ForexFactory — {total} events this week"
            if total else "ForexFactory — feed unavailable"
        )
        _render_banner()
        _render_events()

    _refresh()
    # Mostly cheap: the feed is cached for 30 min, so 59 of every 60 ticks only
    # recompute countdowns and the blackout banner against the cached payload.
    # The sixtieth finds the cache stale and fetches ForexFactory over the
    # network -- and on a synchronous timer that fetch ran ON THE EVENT LOOP,
    # stalling every page in the app for its duration. Warming the cache in a
    # worker thread first means the render below always hits a warm cache, so
    # no tick can block the UI on the feed.
    #
    # The lambda is deliberate: `_refresh` takes `force`, and handing it the
    # poll's data directly would pass a truthy events list as force=True and
    # invalidate the cache on every single tick.
    poll(30.0, nc.get_events, lambda _events: _refresh())
