"""Active positions: the open-trade cards, including remote-node trades."""
import asyncio
from nicegui import ui
from backend.src.controllers.trading import controller as trading_ctl
from backend.src.utils.models import (
    STRATEGY_NAMES,
    STRATEGY_SCALE_OUT,
)
from backend.src.controllers.sync import client as sync_client
from backend.src.controllers.history.controller import (  # noqa: E402,F401
    trade_channel_label, trade_source_label,
)

# Sibling sections of this page.
from ._shared import (
    _pnl_colour,
    _stat_cell,
    _uk,
)


def _render_remote_trade_card(pos: dict, remote: dict) -> None:
    """Full-detail card for a trade opened by the *other* Local/Remote node.
    Live price/PnL come from this node's own MT5 bridge (`pos`, same shared
    account); strategy/TP/SL/channel come from the last sync heartbeat
    (`remote`). No action buttons — this node doesn't own the trade, so
    Close/Partial/Sync would fail against a non-existent local DB row."""
    direction  = (remote.get("direction") or pos.get("type", "BUY")).upper()
    strategy   = remote.get("strategy", STRATEGY_SCALE_OUT)
    strat_name = STRATEGY_NAMES.get(strategy, strategy)
    triggered  = set(remote.get("triggered_tps") or [])

    cur_price = float(pos.get("current_price") or 0)
    entry_p   = float(remote.get("entry_price") or pos.get("open_price") or 0)
    profit    = float(pos.get("profit") or 0)
    sl        = remote.get("stop_loss")
    lot_sz    = float(remote.get("lot_size") or pos.get("volume") or 0)
    rem_sz    = float(remote.get("remaining_lots") or pos.get("volume") or 0)
    closed_lots = round(lot_sz - rem_sz, 4)

    with ui.card().classes("w-full bg-gray-800 rounded-lg p-0 overflow-hidden border border-blue-800"):
        with ui.row().classes("w-full px-4 py-2 items-center justify-between").style("background:#1e2433"):
            with ui.row().classes("items-center gap-2"):
                ui.element("div").classes("w-1 h-5 bg-blue-400 rounded")
                ui.label("Active Trade").classes("text-sm font-semibold text-gray-200")
                ui.badge("VPS", color="blue").classes("text-xs")
            with ui.row().classes("items-center gap-2"):
                ui.badge(strat_name, color="amber").classes("text-xs")
                src_label = trade_source_label(remote.get("tg_source", ""))
                ui.badge(src_label, color="purple").classes("text-xs")
                ch = trade_channel_label(remote.get("tg_source", ""))
                if ch and ch != src_label:
                    ui.badge(ch, color="indigo").classes("text-xs")

        with ui.grid(columns=4).classes("w-full px-4 pt-3 gap-x-4 gap-y-2"):
            _stat_cell("MT5 TICKET", str(pos.get("ticket") or "—"))
            _stat_cell("DIRECTION", direction,
                       "text-green-400" if direction == "BUY" else "text-red-400")
            _stat_cell("ENTRY", f"${entry_p:.2f}")
            _stat_cell("UNREALISED", f"${profit:+.2f}" if cur_price else "—",
                       _pnl_colour(profit))

        with ui.grid(columns=6).classes("w-full px-4 pb-1 gap-x-4 gap-y-2"):
            if sl:
                _stat_cell("CURRENT SL", f"${float(sl):.2f}", "text-yellow-400")
            if closed_lots > 0:
                _stat_cell("LOTS", f"{lot_sz:.2f} → {rem_sz:.2f}", "text-orange-300")
            else:
                _stat_cell("LOTS", f"{lot_sz:.2f}")
            if remote.get("open_time"):
                _stat_cell("OPENED", _uk(remote["open_time"]))
            _stat_cell("STRATEGY", strat_name, "text-amber-400")
            ch_name = trade_channel_label(remote.get("tg_source", ""))
            _stat_cell("CHANNEL", ch_name or "—", "text-indigo-300")

        with ui.column().classes("w-full px-4 pb-2 gap-1"):
            ui.label("TAKE PROFITS").classes("text-xs text-gray-500 tracking-wider font-medium mt-1")
            with ui.row().classes("gap-2 flex-wrap"):
                has_any_tp = False
                for n in range(1, 9):
                    tp_val = remote.get(f"tp{n}")
                    if not tp_val:
                        continue
                    has_any_tp = True
                    hit = n in triggered
                    chip_bg = "background:#22c55e" if hit else "background:#374151"
                    val_col = "#86efac" if hit else "#9ca3af"
                    with ui.column().classes("items-center gap-0").style(
                        f"border:1px solid {'#16a34a' if hit else '#4b5563'};"
                        "border-radius:6px; padding:3px 8px;"
                    ).style(chip_bg):
                        ui.label(f"TP{n}").classes("text-xs font-bold text-white")
                        ui.label(f"${float(tp_val):.2f}").classes("text-xs font-mono").style(f"color:{val_col}")
                if not has_any_tp:
                    ui.label("No TP levels set").classes("text-xs text-gray-500 italic")

        if remote.get("sl_moved_to_be"):
            with ui.row().classes("w-full px-4 py-1 items-center gap-2").style("background: rgba(16,185,129,0.1)"):
                ui.label("check_circle").classes("material-icons text-green-400 text-sm")
                ui.label("SL moved to breakeven — trade is risk-free").classes("text-green-400 text-xs")

        with ui.row().classes("w-full px-4 py-2 items-center").style("border-top: 1px solid #374151"):
            ui.label("Managed by the VPS node — actions unavailable here.").classes(
                "text-xs text-blue-300 italic"
            )
def _render_active_trades(engine):
    container = ui.column().classes("w-full gap-4")

    async def refresh():
        # Fetch first, clear second — clearing before these awaits left the
        # container empty for the full bridge round-trip on every 5s tick
        # (worse under bridge latency), producing a visible blank-then-
        # rebuild flicker. Keeping the previous cards on screen until the
        # new data is ready means the swap only happens once, atomically.
        try:
            tick       = await engine.get_tick()
            trades     = await trading_ctl.run_db(engine.get_open_trades)
            untracked  = await engine.get_untracked_mt5_positions()
        except Exception:
            trades, tick, untracked = [], None, []

        container.clear()
        with container:
            if not trades and not untracked:
                ui.label("No open trades.").classes(
                    "text-gray-500 text-sm italic p-6 text-center w-full"
                )
                return

            # ── Untracked MT5 positions (opened directly in MT5, or opened by
            # the other Local/Remote node — check the sync heartbeat before
            # falling back to the bare "untracked" card) ──────────────────────
            sync_cli = sync_client.get_instance()
            for pos in untracked:
                remote = sync_cli.get_remote_open_position(pos.get("ticket")) if sync_cli else None
                if remote:
                    _render_remote_trade_card(pos, remote)
                    continue

                direction = pos.get("type", "BUY").upper()
                cur_price = float(pos.get("current_price") or 0)
                entry_p   = float(pos.get("open_price") or 0)
                lots      = float(pos.get("volume") or 0)
                profit    = float(pos.get("profit") or 0)
                ticket    = pos.get("ticket", "—")
                sl        = float(pos.get("sl") or 0) or None
                dir_col   = "text-green-400" if direction == "BUY" else "text-red-400"
                pnl_col   = "text-green-400" if profit >= 0 else "text-red-400"

                with ui.card().classes("w-full bg-gray-800 rounded-lg p-0 overflow-hidden border border-orange-700"):
                    with ui.row().classes("w-full px-4 py-2 items-center justify-between").style("background:#2a1a0a"):
                        with ui.row().classes("items-center gap-2"):
                            ui.element("div").classes("w-1 h-5 bg-orange-400 rounded")
                            ui.label("MT5 Position").classes("text-sm font-semibold text-gray-200")
                            ui.badge("UNTRACKED", color="orange").classes("text-xs")
                        ui.label(f"#{ticket}").classes("text-xs text-gray-400")

                    with ui.grid(columns=4).classes("w-full px-4 pt-3 pb-3 gap-x-4 gap-y-2"):
                        _stat_cell("DIRECTION", direction, dir_col)
                        _stat_cell("ENTRY", f"${entry_p:.2f}")
                        _stat_cell("LOTS", f"{lots:.2f}")
                        _stat_cell("UNREALISED", f"${profit:+.2f}", pnl_col)
                        if sl:
                            _stat_cell("SL", f"${sl:.2f}", "text-yellow-400")
                        if cur_price:
                            _stat_cell("CURRENT", f"${cur_price:.2f}")

                    with ui.row().classes("px-4 pb-2"):
                        ui.label("Opened directly in MT5 — now being tracked by the app.").classes(
                            "text-xs text-orange-300 italic"
                        )

            rs         = trading_ctl.get_risk_settings()
            dpm_active = bool(rs.get("dpm_enabled", 0))
            _, ooh_now = trading_ctl.get_effective_strategy(rs)
            _ooh_strat = rs.get("ooh_strategy", "conservative") or "conservative"
            _ooh_label = f"OOH: {STRATEGY_NAMES.get(_ooh_strat, _ooh_strat)}"

            for t in trades:
                direction  = t.get("direction", "?")
                strategy   = t.get("strategy", STRATEGY_SCALE_OUT)
                strat_name = (
                    "DPM" if dpm_active
                    else _ooh_label if ooh_now
                    else STRATEGY_NAMES.get(strategy, strategy)
                )
                trade_id   = t["trade_id"]

                current    = None
                unrealized = 0.0
                if tick:
                    current    = tick.bid if direction == "BUY" else tick.ask
                    unrealized = engine.pnl(
                        direction, float(t["entry_price"]), current,
                        float(t["remaining_lots"]),
                    )

                triggered = await engine.get_triggered_tps(trade_id)

                # ── Single full-width trade card ──────────────────────────────
                with ui.card().classes(
                    "w-full bg-gray-800 rounded-lg p-0 overflow-hidden"
                ):
                        # Header bar
                        with ui.row().classes(
                            "w-full px-4 py-2 items-center justify-between"
                        ).style("background:#1e2433"):
                            with ui.row().classes("items-center gap-2"):
                                ui.element("div").classes("w-1 h-5 bg-yellow-400 rounded")
                                ui.label("Active Trade").classes(
                                    "text-sm font-semibold text-gray-200"
                                )
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(strat_name, color="blue" if dpm_active else "amber").classes("text-xs")
                                src_label = trade_source_label(t.get("tg_source", ""))
                                ui.badge(src_label, color="purple").classes("text-xs")
                                ch = trade_channel_label(t.get("tg_source", ""))
                                if ch and ch != src_label:
                                    ui.badge(ch, color="indigo").classes("text-xs")

                        # Stats grid — core trade metrics
                        with ui.grid(columns=4).classes("w-full px-4 pt-3 gap-x-4 gap-y-2"):
                            _stat_cell("MT5 TICKET", str(t.get("mt5_ticket") or "—"))
                            _stat_cell(
                                "DIRECTION", direction,
                                "text-green-400" if direction == "BUY" else "text-red-400",
                            )
                            _stat_cell("ENTRY", f"${float(t['entry_price']):.2f}")
                            _stat_cell(
                                "UNREALISED",
                                f"${unrealized:+.2f}" if current else "—",
                                _pnl_colour(unrealized),
                            )

                        with ui.grid(columns=6).classes("w-full px-4 pb-1 gap-x-4 gap-y-2"):
                            _stat_cell(
                                "CURRENT SL",
                                f"${float(t['stop_loss']):.2f}",
                                "text-yellow-400",
                            )
                            lot_sz  = float(t["lot_size"])
                            rem_sz  = float(t["remaining_lots"])
                            closed_lots = round(lot_sz - rem_sz, 4)
                            if closed_lots > 0:
                                _stat_cell(
                                    "LOTS",
                                    f"{lot_sz:.2f} → {rem_sz:.2f}",
                                    "text-orange-300",
                                )
                            else:
                                _stat_cell("LOTS", f"{lot_sz:.2f}")
                            _stat_cell("OPENED", _uk(t["open_time"]))
                            _stat_cell("STRATEGY", strat_name,
                                       "text-blue-400" if dpm_active else "text-amber-400")
                            # Take Profit At (profit_close_usd) — show if set
                            rs_now = trading_ctl.get_risk_settings()
                            pcu = float(rs_now.get("profit_close_usd", 0) or 0)
                            if pcu > 0:
                                _stat_cell("CLOSE AT", f"${pcu:.2f}", "text-green-400")
                            else:
                                ch_name = trade_channel_label(t.get("tg_source", ""))
                                _stat_cell("CHANNEL", ch_name or "—", "text-indigo-300")

                        # TP levels with values
                        with ui.column().classes("w-full px-4 pb-2 gap-1"):
                            ui.label("TAKE PROFITS").classes(
                                "text-xs text-gray-500 tracking-wider font-medium mt-1"
                            )
                            with ui.row().classes("gap-2 flex-wrap"):
                                has_any_tp = False
                                for n in range(1, 9):
                                    tp_val = t.get(f"tp{n}")
                                    if not tp_val:
                                        continue
                                    has_any_tp = True
                                    hit = n in triggered
                                    chip_bg  = "background:#22c55e" if hit else "background:#374151"
                                    val_col  = "#86efac" if hit else "#9ca3af"
                                    with ui.column().classes("items-center gap-0").style(
                                        f"border:1px solid {'#16a34a' if hit else '#4b5563'};"
                                        "border-radius:6px; padding:3px 8px;"
                                    ).style(chip_bg):
                                        ui.label(f"TP{n}").classes(
                                            "text-xs font-bold text-white"
                                        )
                                        ui.label(f"${float(tp_val):.2f}").classes(
                                            "text-xs font-mono"
                                        ).style(f"color:{val_col}")
                                if not has_any_tp:
                                    ui.label("No TP levels set").classes("text-xs text-gray-500 italic")

                        # SL at breakeven banner
                        if t.get("sl_moved_to_be"):
                            with ui.row().classes(
                                "w-full px-4 py-1 items-center gap-2"
                            ).style("background: rgba(16,185,129,0.1)"):
                                ui.label("check_circle").classes(
                                    "material-icons text-green-400 text-sm"
                                )
                                ui.label(
                                    "SL moved to breakeven — trade is risk-free"
                                ).classes("text-green-400 text-xs")

                        # DPM live status row
                        dpm_on = bool(trading_ctl.get_risk_settings().get("dpm_enabled", 0))
                        if dpm_on:
                            dpm_status = trading_ctl.get_app_config(
                                f"dpm_status_{trade_id}"
                            ) or "Analysing market..."
                            with ui.row().classes(
                                "w-full px-4 py-1.5 items-start gap-2 flex-wrap"
                            ).style("background: rgba(59,130,246,0.08)"):
                                ui.icon("psychology").classes("text-blue-400 text-sm mt-0.5 shrink-0")
                                ui.label(dpm_status).classes(
                                    "text-blue-300 text-xs leading-relaxed"
                                )

                        # Action bar pinned to bottom
                        with ui.row().classes(
                            "w-full px-4 py-2 gap-2 items-center mt-auto"
                        ).style("border-top: 1px solid #374151"):
                            async def do_close(tid=trade_id):
                                try:
                                    await engine.close_trade(tid, "manual_close")
                                    ui.notify("Trade closed", type="positive")
                                    await refresh()
                                except Exception as e:
                                    ui.notify(str(e), type="negative")

                            ui.button("Close Trade", on_click=do_close).classes(
                                "bg-red-700 text-white text-xs px-3 py-1"
                            )

                            partial_lots = ui.number(
                                value=round(float(t["lot_size"]) * 0.2, 2),
                                min=0.01, step=0.01, format="%.2f",
                            ).classes("w-20 text-xs")

                            async def do_partial(tid=trade_id, pl_inp=partial_lots):
                                try:
                                    tkick = await engine.get_tick()
                                    if not tkick:
                                        raise RuntimeError("No tick")
                                    trow = next(
                                        (x for x in engine.get_open_trades() if x["trade_id"] == tid),
                                        None,
                                    )
                                    if not trow:
                                        raise ValueError("Trade not found")
                                    cp = tkick.bid if trow["direction"] == "BUY" else tkick.ask
                                    await engine.partial_close_trade(tid, float(pl_inp.value), cp, "manual_partial")
                                    ui.notify(f"Partial close: {pl_inp.value} lots", type="info")
                                    await refresh()
                                except Exception as e:
                                    ui.notify(str(e), type="negative")

                            ui.button("Partial Close", on_click=do_partial).classes(
                                "bg-gray-600 text-white text-xs px-2 py-1"
                            )

                            async def do_sync(tid=trade_id):
                                try:
                                    trow = next(
                                        (x for x in engine.get_open_trades() if x["trade_id"] == tid),
                                        None,
                                    )
                                    if trow and trow.get("mt5_ticket"):
                                        await engine.sync_profit(tid, int(trow["mt5_ticket"]))
                                        ui.notify("Synced with MT5", type="positive")
                                        await refresh()
                                except Exception as e:
                                    ui.notify(str(e), type="negative")

                            ui.button("Sync MT5", on_click=do_sync).classes(
                                "text-blue-400 text-xs underline bg-transparent px-2 py-1"
                            )


    ui.timer(5.0, refresh)
    asyncio.ensure_future(refresh())
