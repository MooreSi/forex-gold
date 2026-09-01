"""The closed-trades table."""
import asyncio
import time
from typing import Optional

from nicegui import ui

from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers.history_controller import (
    CONTRACT_SIZE,
)

from ._shared import _entry_deal_comments


def _render_trade_table(engine):
    # The six ticket-map builders moved verbatim to
    # backend.src.controllers.history_controller (M3 page drain).

    with ui.card().classes("w-full bg-gray-800 rounded-lg overflow-hidden"):
        with ui.row().classes("items-center justify-between px-4 py-2 flex-wrap gap-2"):
            ui.label("Closed Trades").classes("font-semibold text-yellow-300")
            status_lbl = ui.label("").classes("text-xs text-orange-400 italic")
            days_sel = ui.select(
                {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days",
                 60: "Last 60 days", 90: "Last 90 days", 180: "Last 6 months"},
                value=7, label="Period",
            ).classes("w-36")
            src_badge = ui.badge("MT5", color="green").classes("text-xs")
            refresh_btn = ui.button("Refresh", icon="refresh").classes(
                "bg-gray-700 text-white text-xs px-3 py-1"
            )

        # Create the table ONCE — update rows in-place rather than destroying/recreating.
        _columns = [
            {"name": "time",      "label": "Closed",      "field": "time",      "align": "left",   "sortable": True},
            {"name": "ticket",    "label": "Ticket",      "field": "ticket",    "align": "right"},
            {"name": "channel",   "label": "Channel",     "field": "channel",   "align": "left"},
            {"name": "order_type","label": "Order Type",  "field": "order_type","align": "center"},
            {"name": "direction", "label": "Dir",         "field": "direction", "align": "center", "sortable": True},
            {"name": "entry",     "label": "Entry",       "field": "entry",     "align": "right"},
            {"name": "exit",      "label": "Exit",        "field": "exit",      "align": "right"},
            {"name": "pips",      "label": "Pips",        "field": "pips",      "align": "right",  "sortable": True},
            {"name": "lots",      "label": "Lots",        "field": "lots",      "align": "right"},
            {"name": "strategy",  "label": "Strategy",    "field": "strategy",  "align": "center"},
            {"name": "spread",    "label": "Spread",      "field": "spread",    "align": "right",  "sortable": True},
            {"name": "fees",      "label": "Fees",        "field": "fees",      "align": "right",  "sortable": True},
            {"name": "pnl",       "label": "Net P&L",     "field": "pnl",       "align": "right",  "sortable": True},
            {"name": "reason",    "label": "Reason",      "field": "reason",    "align": "center"},
            {"name": "rr",        "label": "R:R",         "field": "rr",        "align": "right",  "sortable": True},
            {"name": "max_tp",    "label": "Max TP",      "field": "max_tp",    "align": "center", "sortable": True},
            {"name": "duration",  "label": "Held",        "field": "duration",  "align": "right",  "sortable": True},
            {"name": "pending",   "label": "Pending For", "field": "pending",   "align": "right",  "sortable": True},
        ]
        trade_table = ui.table(
            columns=_columns, rows=[], row_key="_id"
        ).classes("w-full").props("dense flat dark")
        # Adaptive Runner ladder legs collapse into one row per signal (see
        # _ticket_group_map) — clicking that row expands/collapses its legs.
        _expanded_groups: set[str] = set()

        def _on_row_click(e):
            row = e.args[1] if isinstance(e.args, list) and len(e.args) > 1 else (e.args or {})
            gid = row.get("_group_id") if isinstance(row, dict) else None
            if not gid:
                return
            if gid in _expanded_groups:
                _expanded_groups.discard(gid)
            else:
                _expanded_groups.add(gid)
            asyncio.create_task(refresh_table())

        trade_table.on("rowClick", _on_row_click)

        trade_table.add_slot("body-cell-ticket", """
            <q-td :props="props">
                <span v-if="props.row._is_group"
                      style="font-weight:600;color:#fbbf24;cursor:pointer;">
                    {{ props.value }}
                </span>
                <span v-else-if="props.row._is_leg" style="color:#9ca3af;font-size:12px;">
                    {{ props.value }}
                </span>
                <span v-else>{{ props.value }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-direction", """
            <q-td :props="props">
                <q-badge :color="props.value === 'BUY' ? 'green' : 'red'" :label="props.value"/>
            </q-td>
        """)
        trade_table.add_slot("body-cell-fees", """
            <q-td :props="props">
                <span :class="{'text-gray-400': props.value === '$0.00', 'text-orange-400': props.value !== '$0.00'}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-pnl", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-pips", """
            <q-td :props="props">
                <span :class="{'text-green-400': parseFloat(props.value) >= 0, 'text-red-400': parseFloat(props.value) < 0, 'text-gray-400': props.value === '—'}">
                    {{ props.value }}
                </span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-reason", """
            <q-td :props="props">
                <span :class="{
                    'text-red-400':   props.value === 'SL',
                    'text-green-400': props.value === 'TP' || props.value === 'Partial TP',
                    'text-amber-400': props.value === 'Manual',
                    'text-orange-400': props.value === 'Stop-out',
                }">{{ props.value || '—' }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-rr", """
            <q-td :props="props">
                <span v-if="props.value === '—'" style="color:#6b7280;">—</span>
                <span v-else style="color:#93c5fd;">{{ props.value }}</span>
            </q-td>
        """)
        trade_table.add_slot("body-cell-max_tp", """
            <q-td :props="props">
                <span v-if="props.value === '...'"
                      style="color:#6b7280;font-size:11px;" title="Updating in 30 min">...</span>
                <span v-else-if="props.value === 'none'"
                      style="color:#6b7280;">—</span>
                <span v-else-if="props.value === 'n/a'" style="color:#4b5563;"
                      title="No TP ladder to measure this position against">—</span>
                <q-badge v-else-if="props.value"
                         :label="props.value"
                         :color="props.value === 'TP1' ? 'teal' : props.value === 'TP2' ? 'green' : 'purple'"
                         style="font-size:11px;font-weight:600;"/>
                <span v-else style="color:#374151;font-size:10px;" title="30-min window not yet elapsed">–</span>
            </q-td>
        """)

        async def refresh_table():
            """Fetch closed trades exclusively from MT5 deal history."""
            mt5_error: Optional[str] = None
            rows_by_ticket: dict[int, dict] = {}
            _leg_comments: dict = {}
            # Offloaded — these are all synchronous DB reads; running them
            # directly on the event loop blocked the whole app every 15s.
            _days_now  = int(days_sel.value)
            src_map    = await history_ctl.ticket_source_map(_days_now)
            strat_map  = await history_ctl.ticket_strategy_map(_days_now)
            max_tp_map = await history_ctl.ticket_max_tp_map()
            rr_map     = await history_ctl.ticket_rr_map()
            order_type_map = await history_ctl.ticket_order_type_map(_days_now)
            comm_rate  = await history_ctl.platform_fee_rate()
            try:
                deals = await engine._bridge.get_deal_history(int(days_sel.value)) or []
                if deals:
                    by_pos: dict[int, list] = {}
                    for d in deals:
                        pid = d.get("position_id")
                        if pid:  # excludes None and 0 (balance/deposit ops)
                            by_pos.setdefault(int(pid), []).append(d)

                    # Positions with no local trade row of their own: attribute
                    # them from the comment the opening order carried (EA
                    # Template sibling legs, orphaned "sig:" rows, and the
                    # third-party copier EA) -- see
                    # _comment_attribution_maps. setdefault, so a real local
                    # row always wins over a comment inference.
                    _leg_comments = _entry_deal_comments(by_pos)
                    if _leg_comments:
                        _leg_src, _leg_strat, _leg_max_tp = await history_ctl.comment_attribution_maps(_leg_comments)
                        for _t, _v in _leg_src.items():
                            src_map.setdefault(_t, _v)
                        for _t, _v in _leg_strat.items():
                            strat_map.setdefault(_t, _v)
                        for _t, _v in _leg_max_tp.items():
                            max_tp_map.setdefault(_t, _v)

                    # Spread paid at entry is a historical fact — it never changes once
                    # computed, so cache it permanently and only ask MT5 for tickets
                    # this table has never seen before (this loop re-runs every 15s).
                    spread_cache = await history_ctl.get_cached_spreads(
                        list(by_pos.keys())
                    )

                    # Fetch every still-uncached ticket's entry-time tick concurrently
                    # instead of one sequential await per ticket inside the row loop
                    # below — with a wide date range (or a cold cache) that loop was
                    # awaiting hundreds of individual MT5 bridge round-trips one at a
                    # time, each blocking the next.
                    _needs_spread: list[tuple[int, float, float]] = []  # (ticket, open_ts, open_lots)
                    for ticket, pos_deals in by_pos.items():
                        if spread_cache.get(ticket) is not None:
                            continue
                        _open = next((d for d in pos_deals if d.get("entry") == 0), None)
                        if not _open:
                            continue
                        _open_ts = float(_open.get("time", 0))
                        if _open_ts:
                            _needs_spread.append((ticket, _open_ts, float(_open.get("volume", 0))))

                    if _needs_spread:
                        _ticks = await asyncio.gather(
                            *(engine._bridge.get_tick_at(ts) for _, ts, _ in _needs_spread),
                            return_exceptions=True,
                        )
                        for (ticket, _ts, open_lots), tick_at in zip(_needs_spread, _ticks):
                            if not tick_at or isinstance(tick_at, BaseException):
                                continue
                            sp_price = round(float(tick_at["ask"]) - float(tick_at["bid"]), 5)
                            sp_points = round(sp_price / 0.01, 1)
                            sp_cost = round(sp_price * open_lots * CONTRACT_SIZE, 2)
                            spread_cache[ticket] = {
                                "spread_price": sp_price, "spread_points": sp_points,
                                "spread_cost_usd": sp_cost,
                            }
                            await history_ctl.cache_spread(
                                ticket, sp_price, sp_points, sp_cost
                            )

                    for ticket, pos_deals in by_pos.items():
                        open_deal  = next((d for d in pos_deals if d.get("entry") == 0), None)
                        _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
                        close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
                        if not close_deal:
                            continue

                        if open_deal:
                            direction = "BUY" if int(open_deal.get("type", 0)) == 0 else "SELL"
                            entry     = float(open_deal.get("price", 0))
                            open_lots = float(open_deal.get("volume", 0))
                        else:
                            close_type = int(close_deal.get("type", 0))
                            direction  = "SELL" if close_type == 0 else "BUY"
                            entry      = 0.0
                            open_lots  = float(close_deal.get("volume", 0))

                        exit_p       = float(close_deal.get("price", 0))
                        pnl, fees_display = history_ctl.apply_fee(pos_deals, open_lots, comm_rate)
                        close_ts = float(close_deal.get("time", 0))
                        open_ts  = float(open_deal.get("time", 0)) if open_deal else 0.0
                        reason   = history_ctl.parse_reason(close_deal.get("comment") or "", pnl)

                        # Order Type / Pending For: distinguishes a genuine
                        # resting Limit Runner/EA Template grid fill from an
                        # immediate market open, and shows how long it sat on
                        # the broker's book before it filled -- so a widening
                        # gap between placement and fill (stale pending
                        # orders) can be spotted and weighed against its P&L.
                        _otype, _pending_at = order_type_map.get(str(ticket), ("market", None))
                        order_type_display = "Limit" if _otype == "limit" else "Market"
                        pending_display = (
                            history_ctl.format_duration(open_ts - _pending_at)
                            if _pending_at and open_ts else "—"
                        )
                        duration_display = history_ctl.format_duration(close_ts - open_ts) if open_ts and close_ts else "—"

                        # Spread paid at entry — already embedded in pnl via MT5's real
                        # fill prices; shown here as an informational cost breakdown, not
                        # a further deduction (Vantage Standard STP: 0% commission, spread-only).
                        spread_display = "—"
                        cached_spread = spread_cache.get(ticket)
                        if cached_spread:
                            spread_display = f"{cached_spread['spread_points']:.1f}pt"
                            fees_display = cached_spread["spread_cost_usd"]

                        # Pips: Vantage XAUUSD — 1 pip = $0.10 price change
                        if entry and exit_p:
                            raw_pips = (exit_p - entry) if direction == "BUY" else (entry - exit_p)
                            pips_val = raw_pips * 10
                            pips_str = f"{pips_val:+.1f}"
                        else:
                            pips_val = 0.0
                            pips_str = "—"

                        # Show partial close breakdown in the lots column
                        close_vols = [float(d.get("volume", 0)) for d in _cd]
                        if len(_cd) > 1:
                            fills_str = " + ".join(f"{v:.2f}" for v in close_vols)
                            lots_display = f"{open_lots:.2f} (P/C: {fills_str})"
                        else:
                            lots_display = f"{open_lots:.2f}"

                        # Max TP: blank = 30-min window not yet elapsed,
                        # "..." = window elapsed but computation pending,
                        # "none"/TP label = computed result.
                        _now = time.time()
                        _max_tp_raw = max_tp_map.get(str(ticket))
                        if _max_tp_raw is not None:
                            max_tp_display = _max_tp_raw   # "none" or "TP1".."TP8"
                        elif close_ts and (_now - close_ts) >= 1800:
                            max_tp_display = "..."          # window elapsed, engine pending
                        else:
                            max_tp_display = ""             # 30-min window not yet up

                        _rr_raw = rr_map.get(str(ticket))
                        rr_display = f"{_rr_raw:.2f}:1" if _rr_raw is not None else "—"

                        rows_by_ticket[ticket] = {
                            "_ts":       close_ts,
                            "_pnl":      pnl,
                            "_pips":     pips_val,
                            "_lots":     open_lots,
                            "_fees":     fees_display,
                            "_entry":    entry,
                            "_id":       str(ticket),
                            "time":      history_ctl.format_broker_ts(close_ts),
                            "ticket":    str(ticket),
                            "channel":   src_map.get(str(ticket), "—"),
                            "order_type": order_type_display,
                            "direction": direction,
                            "entry":     f"{entry:.2f}" if entry else "—",
                            "exit":      f"{exit_p:.2f}",
                            "pips":      pips_str,
                            "strategy":  strat_map.get(str(ticket), "—"),
                            "lots":      lots_display,
                            "spread":    spread_display,
                            "fees":      f"${fees_display:.2f}",
                            "pnl":       f"{pnl:+.2f}",
                            "reason":    reason,
                            "rr":        rr_display,
                            "max_tp":    max_tp_display,
                            "duration":  duration_display,
                            "pending":   pending_display,
                        }
            except Exception as exc:
                mt5_error = str(exc)

            group_map = await history_ctl.ticket_group_map()
            if _leg_comments:
                tpl_group_map = await history_ctl.template_group_map(_leg_comments)
                for _t, _v in tpl_group_map.items():
                    group_map.setdefault(_t, _v)
            groups: dict[str, list[dict]] = {}
            flat_rows: list[dict] = []
            for ticket, row in rows_by_ticket.items():
                gid, tier = group_map.get(str(ticket), (None, None))
                if gid:
                    row["_tier"] = tier
                    groups.setdefault(gid, []).append(row)
                else:
                    flat_rows.append(row)

            for gid, members in groups.items():
                if len(members) == 1:
                    flat_rows.append(members[0])
                    continue
                members.sort(key=lambda r: r["_tier"])
                anchor = next((m for m in members if m["_tier"] == 1), members[0])
                total_lots  = sum(m["_lots"] for m in members)
                total_pnl   = sum(m["_pnl"] for m in members)
                total_fees  = sum(m["_fees"] for m in members)
                tp_hits     = [m for m in members if m["reason"] == "TP"]
                last_close  = max(members, key=lambda m: m["_ts"])
                weighted_pips = (
                    sum(m["_pips"] * m["_lots"] for m in members) / total_lots
                    if total_lots else 0.0
                )
                expanded = gid in _expanded_groups
                arrow = "▾" if expanded else "▸"
                flat_rows.append({
                    "_ts":         last_close["_ts"],
                    "_id":         f"group:{gid}",
                    "_is_group":   True,
                    "_group_id":   gid,
                    "_leg_count":  len(members),
                    "time":        last_close["time"],
                    "ticket":      f"{arrow} {len(members)} legs",
                    "channel":     anchor["channel"],
                    "order_type":  anchor["order_type"],
                    "direction":   anchor["direction"],
                    "entry":       anchor["entry"],
                    "exit":        "Multiple",
                    "pips":        f"{weighted_pips:+.1f}",
                    "strategy":    anchor["strategy"],
                    "lots":        f"{total_lots:.2f}",
                    "spread":      "—",
                    "fees":        f"${total_fees:.2f}",
                    "pnl":         f"{total_pnl:+.2f}",
                    "reason":      f"{len(tp_hits)}/{len(members)} TP",
                    "rr":          anchor["rr"],
                    "max_tp":      anchor["max_tp"],
                    "duration":    anchor["duration"],
                    "pending":     anchor["pending"],
                })
                if expanded:
                    for m in members:
                        m["_id"] = f"leg:{m['ticket']}"
                        m["_is_leg"] = True
                        m["_group_id"] = gid
                        m["ticket"] = f"↳ {m['ticket']} (T{m['_tier']})"
                        flat_rows.append(m)

            rows = sorted(flat_rows, key=lambda r: r["_ts"], reverse=True)

            src_badge.props("color=green" if rows else "color=grey")
            src_badge.text = f"MT5 ({len(rows)})" if rows else "MT5"
            status_lbl.text = f"MT5 error: {mt5_error}" if mt5_error else ""

            trade_table.rows = rows
            trade_table.update()

        async def _on_refresh_click():
            await refresh_table()

        days_sel.on("update:model-value", lambda _: asyncio.create_task(refresh_table()))
        refresh_btn.on("click", _on_refresh_click)
        ui.timer(15.0, refresh_table)
        asyncio.ensure_future(refresh_table())
