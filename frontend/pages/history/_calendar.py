"""The monthly calendar: per-day results and the day-detail breakdown."""
import asyncio
import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo as _ZoneInfo

from nicegui import ui

from backend.src.controllers import history_controller as history_ctl
from frontend.components.empty_state import render_empty_state

from ._shared import (
    _SESSION_LABELS,
    _broker_ts_to_utc_hour,
    _entry_deal_comments,
    _get_market_type_map,
)

import logging

_log = logging.getLogger(__name__)


def _render_calendar(engine):
    _state = {"year": datetime.now(_ZoneInfo("Europe/London")).year, "month": datetime.now(_ZoneInfo("Europe/London")).month}

    header_row  = ui.row().classes("w-full items-center gap-3 mb-3")
    stats_lbl   = ui.label("Loading...").classes("text-sm text-gray-400")
    source_lbl  = ui.label("").classes("text-xs text-gray-500 ml-2")
    content     = ui.column().classes("w-full")

    _detail_store: dict = {}   # {date: [ {ticket, source, strategy, direction, pnl}, ... ]}

    # _ticket_info moved verbatim to the history controller (M3 page drain).

    async def _build_day_map(year: int, month: int) -> tuple[dict, dict, str]:
        """Build day-level P&L map exclusively from MT5 deal history."""
        trade_map: dict[int, tuple[date, float]] = {}  # ticket → (date, pnl)
        _dir_by_ticket: dict[str, str] = {}   # from the broker's own opening deal
        _close_hour_by_ticket: dict[int, int] = {}  # ticket → UTC hour of close
        _detail_store.clear()
        # Offloaded — both are synchronous DB reads.
        _tinfo    = await history_ctl.ticket_info()
        comm_rate = await history_ctl.platform_fee_rate()
        _leg_comments: dict = {}

        try:
            today_d   = datetime.now(_ZoneInfo("Europe/London")).date()
            first     = date(year, month, 1)
            days_back = max((today_d - first).days + 35, 35)
            deals     = await engine._bridge.get_deal_history(int(days_back)) or []

            if deals:
                by_pos: dict[int, list] = {}
                for d in deals:
                    pid = d.get("position_id")
                    if pid:  # excludes None and 0 (balance/deposit ops)
                        by_pos.setdefault(int(pid), []).append(d)

                _leg_comments = _entry_deal_comments(by_pos)

                for ticket, pos_deals in by_pos.items():
                    _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
                    close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
                    if not close_deal:
                        continue
                    close_ts = close_deal.get("time")
                    if not close_ts:
                        continue
                    d_date = history_ctl.broker_ts_to_uk_date(close_ts)
                    if not d_date or d_date.year != year or d_date.month != month:
                        continue
                    # Net P&L including estimated fees (apply_fee), matching the
                    # Closed Trades table and equity curve — not raw MT5 profit,
                    # which is always fee-free on a demo account and would
                    # understate real trading cost everywhere else in this file.
                    open_deal = next((d for d in pos_deals if d.get("entry") == 0), None)
                    open_lots = float(open_deal.get("volume", 0)) if open_deal else float(close_deal.get("volume", 0))
                    pnl, _fees = history_ctl.apply_fee(pos_deals, open_lots, comm_rate)
                    trade_map[ticket] = (d_date, pnl)
                    _ch = _broker_ts_to_utc_hour(close_ts)
                    if _ch is not None:
                        _close_hour_by_ticket[ticket] = _ch
                    if open_deal is not None:
                        _dir_by_ticket[str(ticket)] = (
                            "BUY" if int(open_deal.get("type", 0)) == 0 else "SELL"
                        )
        except Exception as e:
            _log.debug("[history] day-map direction lookup failed: %s", e)

        # Same comment-based attribution the Closed Trades table performs --
        # without it every EA Template sibling leg (the bulk of a grid trade's
        # broker positions) showed "Unknown" here while the table beside it
        # named the channel correctly. Only fills tickets the DB maps above
        # could not resolve, so a real local row always wins.
        if _leg_comments:
            _c_src, _c_strat, _ = await history_ctl.comment_attribution_maps(
                _leg_comments)
            for _t, _v in _c_src.items():
                if _t not in _tinfo:
                    _tinfo[_t] = (_v, _c_strat.get(_t, "—"), _dir_by_ticket.get(_t, ""))

        day_map: dict[date, dict] = {}
        for _ticket, (d, pnl) in trade_map.items():
            if d not in day_map:
                day_map[d] = {"pnl": 0.0, "trades": 0, "wins": 0}
            day_map[d]["pnl"]    += pnl
            day_map[d]["trades"] += 1
            if pnl > 0:
                day_map[d]["wins"] += 1
            src, strat, dir_ = _tinfo.get(
                str(_ticket), ("Unknown", "—", _dir_by_ticket.get(str(_ticket), "")))
            _detail_store.setdefault(d, []).append({
                "ticket": _ticket, "source": src, "strategy": strat,
                "direction": dir_, "pnl": pnl,
                # Kept so the day view can split by trading session as well as
                # by channel -- the session is derived from the close, the same
                # event that decides which calendar day the trade lands on.
                "utc_hour": _close_hour_by_ticket.get(_ticket),
            })

        source = f"MT5 ({len(trade_map)} trades)" if trade_map else "no data"
        market_map = _get_market_type_map(year, month)
        return day_map, market_map, source

    _sun_cal = calendar.Calendar(firstweekday=6)  # Sunday-first

    def _pnl_fmt(v: float) -> str:
        if abs(v) < 10000:
            return f"${v:+,.2f}"
        return f"${v/1000:+.1f}K"

    # ── Persistent day-detail dialog ──────────────────────────────────────────
    # Created once here (outside content/grid) so that content.clear() on
    # auto-refresh never destroys it while it is open.
    _day_dialog = ui.dialog().props("persistent")
    with _day_dialog:
        with ui.card().classes("bg-gray-900 p-4 rounded-lg").style("min-width:560px"):
            _dialog_body = ui.column().classes("w-full gap-1")
            ui.button("Close", on_click=_day_dialog.close).classes(
                "bg-gray-700 text-white mt-3"
            )

    def _show_day_detail(d: date):
        """Populate and open the persistent day-detail dialog."""
        trades = _detail_store.get(d, [])
        _dialog_body.clear()
        with _dialog_body:
            day_total = sum(t["pnl"] for t in trades)
            wins      = sum(1 for t in trades if t["pnl"] > 0)
            wr        = wins / len(trades) * 100 if trades else 0.0
            with ui.row().classes("items-center justify-between w-full mb-2"):
                ui.label(d.strftime("%A, %d %B %Y")).classes("text-base font-bold text-yellow-300")
                ui.label(_pnl_fmt(day_total)).classes(
                    "text-base font-bold font-mono " +
                    ("text-green-400" if day_total >= 0 else "text-red-400")
                )
            if not trades:
                render_empty_state("day_trades", compact=True)
                _day_dialog.open()
                return
            ui.label(f"{len(trades)} trades · {wr:.0f}% win rate").classes("text-xs text-gray-400 mb-2")

            # ── Breakdowns: where the day's result actually came from ─────────
            def _tally(key_fn) -> dict:
                out: dict = {}
                for t in trades:
                    k = key_fn(t)
                    if k is None:
                        continue
                    s = out.setdefault(k, {"n": 0, "w": 0, "pnl": 0.0})
                    s["n"] += 1
                    s["pnl"] += t["pnl"]
                    if t["pnl"] > 0:
                        s["w"] += 1
                return out

            def _breakdown_table(title: str, first_col: str, tally: dict,
                                 order=None, note: str = "") -> None:
                """One breakdown table. `order` fixes the row order (used for
                sessions, which read better chronologically); without it rows
                sort by P&L, best first."""
                if not tally:
                    return
                ui.label(title).classes(
                    "text-xs font-semibold text-gray-300 uppercase tracking-wider mt-1")
                if note:
                    ui.label(note).classes("text-xs text-gray-500 mb-1")
                if order is not None:
                    items = [(k, tally[k]) for k in order if k in tally]
                else:
                    items = sorted(tally.items(), key=lambda x: -x[1]["pnl"])
                rows_ = [
                    {"k": k,
                     # Wins as "3 / 5" rather than a bare percentage: on a day
                     # with two trades a "50%" tells you almost nothing, and
                     # this panel is most useful on exactly those days.
                     "wins": f"{v['w']} / {v['n']}",
                     "win_rate": f"{(v['w'] / v['n'] * 100 if v['n'] else 0):.0f}%",
                     "pnl": f"${v['pnl']:+.2f}"}
                    for k, v in items
                ]
                ui.table(
                    columns=[
                        {"name": "k", "label": first_col, "field": "k", "align": "left"},
                        {"name": "wins", "label": "Wins", "field": "wins", "align": "right"},
                        {"name": "win_rate", "label": "Win%", "field": "win_rate", "align": "right"},
                        {"name": "pnl", "label": "Net P&L", "field": "pnl", "align": "right"},
                    ],
                    rows=rows_, row_key="k",
                ).classes("w-full mb-3").props("dense flat dark")

            _breakdown_table("By Signal Source", "Source", _tally(lambda t: t["source"]))

            # Session comes from the close, the same event that decides which
            # calendar day the trade belongs to. Trades whose close time the
            # broker history did not carry are dropped from this split rather
            # than bucketed into a wrong session, and the note says so when it
            # happens, so a short table is never mistaken for a quiet session.
            _sess_names = dict(_SESSION_LABELS)
            _sess_tally = _tally(
                lambda t: _sess_names.get(history_ctl.session_for_hour(t["utc_hour"]))
                if t.get("utc_hour") is not None else None
            )
            _sess_n = sum(v["n"] for v in _sess_tally.values())
            _breakdown_table(
                "By Market Session", "Session", _sess_tally,
                order=[lbl for _k, lbl in _SESSION_LABELS],
                note=("" if _sess_n == len(trades)
                      else f"{len(trades) - _sess_n} trade(s) had no close time and are not shown here"),
            )

            # ── Individual trades ─────────────────────────────────────────────
            ui.label("Trades").classes("text-xs font-semibold text-gray-300 uppercase tracking-wider")
            trade_rows = [
                {"ticket": str(t["ticket"]), "source": t["source"],
                 "direction": t["direction"], "strategy": t["strategy"],
                 "pnl": f"${t['pnl']:+.2f}"}
                for t in sorted(trades, key=lambda x: -x["pnl"])
            ]
            ui.table(
                columns=[
                    {"name": "ticket", "label": "Ticket", "field": "ticket", "align": "left"},
                    {"name": "source", "label": "Source", "field": "source", "align": "left"},
                    {"name": "direction", "label": "Dir", "field": "direction", "align": "center"},
                    {"name": "strategy", "label": "Strategy", "field": "strategy", "align": "left"},
                    {"name": "pnl", "label": "P&L", "field": "pnl", "align": "right"},
                ],
                rows=trade_rows, row_key="ticket",
            ).classes("w-full").props("dense flat dark")
        _day_dialog.open()

    def _draw_grid(day_map: dict, market_map: dict, year: int, month: int):
        content.clear()
        with content:
            # Day-of-week headers — full width grid
            with ui.grid(columns=7).classes("w-full gap-1.5 mb-1"):
                for day_name in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]:
                    ui.label(day_name).classes(
                        "text-center text-xs text-white font-semibold py-1"
                    )

            cal   = _sun_cal.monthdayscalendar(year, month)
            today = datetime.now(_ZoneInfo("Europe/London")).date()

            for week in cal:
                with ui.grid(columns=7).classes("w-full gap-1.5 mb-1.5"):
                    for day_num in week:
                        if day_num == 0:
                            ui.element("div").style("min-height:88px")
                            continue
                        d    = date(year, month, day_num)
                        info = day_map.get(d)
                        is_today = (d == today)
                        today_ring = "outline:2px solid #3b82f6;" if is_today else ""

                        mkt = market_map.get(d)
                        mkt_label, mkt_colour = mkt if mkt else ("", "")

                        if info:
                            pnl      = info["pnl"]
                            trades_n = info["trades"]
                            wins     = info["wins"]
                            wr       = wins / trades_n * 100 if trades_n else 0.0
                            is_green = pnl >= 0
                            bg     = "#0d2818" if is_green else "#2d0a0a"
                            border = "#16532c" if is_green else "#7f1d1d"
                            pnl_c  = "#4ade80" if is_green else "#f87171"
                            _cell = ui.column().classes(
                                "p-2 rounded gap-0.5 cursor-pointer hover:brightness-125"
                            ).style(
                                f"background:{bg}; border:1px solid {border}; "
                                f"min-height:88px; {today_ring}"
                            )
                            _cell.on("click", lambda _=None, dd=d: _show_day_detail(dd))
                            with _cell:
                                with ui.row().classes("items-center justify-between w-full"):
                                    ui.label(str(day_num)).classes(
                                        "text-xs text-white font-bold"
                                    )
                                    if trades_n > 0:
                                        ui.label(f"{wr:.0f}%").classes(
                                            "text-xs text-white font-semibold"
                                        )
                                ui.label(_pnl_fmt(pnl)).classes(
                                    "text-sm font-bold font-mono"
                                ).style(f"color:{pnl_c}")
                                ui.label(
                                    f"{trades_n} trade{'s' if trades_n != 1 else ''}"
                                ).classes("text-xs text-white")
                                if mkt_label:
                                    ui.label(mkt_label).classes(
                                        "text-xs font-semibold leading-none mt-0.5"
                                    ).style(f"color:{mkt_colour}")
                        else:
                            with ui.column().classes("p-2 rounded gap-0").style(
                                f"background:#0f1117; border:1px solid #1f2937; "
                                f"min-height:88px; {today_ring}"
                            ):
                                ui.label(str(day_num)).classes(
                                    "text-xs font-bold " +
                                    ("text-blue-300" if is_today else "text-white")
                                )
                                if is_today:
                                    ui.label("Today").classes("text-xs text-blue-400 italic")
                                if mkt_label:
                                    ui.label(mkt_label).classes(
                                        "text-xs font-semibold leading-none mt-1"
                                    ).style(f"color:{mkt_colour}")

            # ── Week summary row (below grid) ─────────────────────────────────
            ui.separator().classes("my-3")
            ui.label("Weekly Totals").classes("text-xs text-white font-semibold mb-2 uppercase tracking-wider")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                for wi, week in enumerate(_sun_cal.monthdayscalendar(year, month)):
                    days_in   = [day for day in week if day != 0]
                    if not days_in:
                        continue
                    w_dates   = [date(year, month, d) for d in days_in]
                    w_trading = [d for d in w_dates if d in day_map]
                    w_pnl     = sum(day_map[d]["pnl"] for d in w_trading)
                    w_trades  = sum(day_map[d]["trades"] for d in w_trading)
                    is_green  = w_pnl >= 0
                    col = "text-green-400" if is_green else "text-red-400"
                    bg  = "#0d2818" if is_green else "#2d0a0a"
                    with ui.card().classes("flex-1 min-w-20 rounded p-2").style(
                        f"background:{bg}; border:1px solid {'#16532c' if is_green else '#7f1d1d'}"
                    ):
                        ui.label(f"Week {wi + 1}").classes("text-xs text-white font-semibold")
                        ui.label(_pnl_fmt(w_pnl)).classes(f"text-sm font-bold {col}")
                        ui.label(
                            f"{w_trades} trade{'s' if w_trades != 1 else ''}"
                        ).classes("text-xs text-white")

    # Cache the last-rendered state so background timer reloads skip the
    # expensive clear-and-rebuild when the underlying data hasn't changed.
    _render_cache: dict = {"day_map": None, "market_map": None,
                           "year": None, "month": None}

    async def reload(force: bool = False):
        """Refresh calendar data.  Pass force=True on month navigation so the
        grid always redraws (showing the loading state) even if data is cached."""
        if force:
            stats_lbl.text = "Loading..."
            source_lbl.text = ""

        day_map, market_map, source = await _build_day_map(_state["year"], _state["month"])

        total_pnl    = sum(v["pnl"] for v in day_map.values())
        trading_days = len(day_map)
        total_trades = sum(v["trades"] for v in day_map.values())
        total_wins   = sum(v["wins"]   for v in day_map.values())
        win_rate     = total_wins / total_trades * 100 if total_trades else 0.0

        sign  = "+" if total_pnl >= 0 else ""
        pnl_s = f"${sign}{total_pnl:,.2f}"

        stats_lbl.text = (
            f"Month total: {pnl_s}  |  {trading_days} trading days  |  "
            f"{total_trades} trades  |  {win_rate:.0f}% win rate"
        )
        stats_lbl.classes(
            replace="text-sm font-semibold " +
                    ("text-green-400" if total_pnl >= 0 else "text-red-400")
        )
        source_lbl.text = f"({source})"

        # Only rebuild the DOM grid when data or month has actually changed,
        # preventing the clear→rebuild flicker on every background timer tick.
        cache_key = (_state["year"], _state["month"])
        data_changed = (
            force
            or _render_cache["year"]  != _state["year"]
            or _render_cache["month"] != _state["month"]
            or _render_cache["day_map"] != day_map
        )
        if data_changed:
            _render_cache.update({"day_map": day_map, "market_map": market_map,
                                  "year": _state["year"], "month": _state["month"]})
            _draw_grid(day_map, market_map, _state["year"], _state["month"])

    month_lbl_ref: list = []

    def _update_header():
        if month_lbl_ref:
            month_lbl_ref[0].text = datetime(
                _state["year"], _state["month"], 1
            ).strftime("%B %Y")

    def prev_month():
        if _state["month"] == 1:
            _state["month"], _state["year"] = 12, _state["year"] - 1
        else:
            _state["month"] -= 1
        _update_header()
        asyncio.create_task(reload(force=True))

    def next_month():
        if _state["month"] == 12:
            _state["month"], _state["year"] = 1, _state["year"] + 1
        else:
            _state["month"] += 1
        _update_header()
        asyncio.create_task(reload(force=True))

    def this_month():
        now = datetime.now()
        _state["year"], _state["month"] = now.year, now.month
        _update_header()
        asyncio.create_task(reload(force=True))

    with header_row:
        ui.button(icon="chevron_left",  on_click=prev_month).classes(
            "bg-gray-700 text-white text-xs px-2 py-1"
        )
        lbl = ui.label(
            datetime(_state["year"], _state["month"], 1).strftime("%B %Y")
        ).classes("text-base font-semibold text-gray-200 w-32 text-center")
        month_lbl_ref.append(lbl)
        ui.button(icon="chevron_right", on_click=next_month).classes(
            "bg-gray-700 text-white text-xs px-2 py-1"
        )
        ui.button("This month", on_click=this_month).classes(
            "bg-gray-700 text-white text-xs px-3 py-1 ml-2"
        )
        ui.button(icon="refresh", on_click=lambda: asyncio.create_task(reload(force=True))).classes(
            "bg-blue-700 text-white text-xs px-2 py-1 ml-1"
        ).tooltip("Refresh from MT5")
        ui.space()
        stats_lbl
        source_lbl

    asyncio.ensure_future(reload(force=True))
    ui.timer(15.0, reload)  # silent background poll — only redraws if data changed
