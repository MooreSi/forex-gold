"""The chart's open-trades panel.

244 lines that were a closure inside render() over exactly two names, engine
and trades_panel. Both are keyword-only parameters now, spelled the same, so
the body is unchanged.
"""
from nicegui import ui

from backend.src.controllers import chart_controller
from backend.src.controllers.trading_controller import (
    STRATEGY_NAMES,
)
from backend.src.controllers.trading_controller import (
    STRATEGY_SCALE_OUT,
)
from datetime import datetime
from backend.src.controllers import sync_controller as sync_ctl
from datetime import timezone
from frontend.pages.trading import trade_channel_label
from frontend.pages.trading import trade_source_label

_BEAR_COL  = "#FF4444"   # bright red   (matches old app)
_BULL_COL  = "#00CC88"   # bright green (matches old app)


def _pnl_col(v: float) -> str:
    return "#4ade80" if v >= 0 else "#f87171"


def _untracked_position_node_label() -> str:
    """Which node opened an MT5 position this node's own DB has no record of.

    The Mac and VPS share one MT5 account, so a position missing from THIS
    node's vantage_simulated_trades table was opened by the other, currently-
    active node — the Local/Remote mutual-exclusion gate in engine.py's
    open_trade() guarantees only one node ever opens a given position, so
    this is a reliable inference, not a guess."""
    try:
        host, _, _ = sync_ctl.load_config()
        if host and chart_controller.get_active_trader() == sync_ctl.TRADER_REMOTE_VPS:
            return "Remote"
        return "Local"
    except Exception:
        return "Local"

async def _refresh_trades_panel(
    trades: list[dict], tick, untracked: list[dict] | None = None,
    *, engine, trades_panel,
) -> None:
    trades_panel.clear()
    with trades_panel:
        if not trades and not (untracked or []):
            ui.label("No open trades").classes("text-xs text-gray-500 italic px-2")
            return

        # ── Untracked MT5 positions ───────────────────────────────────────
        # "Untracked" means this node's own DB has no record — the trade was
        # opened by whichever node is the currently active trader. Before
        # falling back to a bare native-MT5-only card, check the sync
        # heartbeat: if the VPS opened this trade, its full detail
        # (strategy, TP ladder, channel, SL) is already mirrored to this
        # node every 3s via get_remote_open_position(), same source
        # trading.py's Active Trades tab already uses for this exact case.
        for pos in (untracked or []):
            ticket = pos.get("ticket", "—")
            remote = sync_ctl.get_remote_open_position(ticket)

            if remote:
                direction  = (remote.get("direction") or pos.get("type", "BUY")).upper()
                strategy   = remote.get("strategy", STRATEGY_SCALE_OUT)
                strat_name = STRATEGY_NAMES.get(strategy, strategy)
                triggered  = set(remote.get("triggered_tps") or [])
                entry_p    = float(remote.get("entry_price") or pos.get("open_price") or 0)
                current_p  = float(pos.get("current_price") or 0)
                profit     = float(pos.get("profit") or 0)
                sl_p       = float(remote.get("stop_loss") or 0) or None
                lot_sz     = float(remote.get("lot_size") or pos.get("volume") or 0)
                rem_sz     = float(remote.get("remaining_lots") or pos.get("volume") or 0)
                closed_lots = round(lot_sz - rem_sz, 4)
                lots_str   = f"{lot_sz:.2f} → {rem_sz:.2f}" if closed_lots > 0 else f"{lot_sz:.2f}"
                lots_col   = "#fb923c" if closed_lots > 0 else "#e5e7eb"
            else:
                direction  = pos.get("type", "BUY").upper()
                strat_name = ""
                triggered  = set()
                entry_p    = float(pos.get("open_price") or 0)
                current_p  = float(pos.get("current_price") or 0)
                profit     = float(pos.get("profit") or 0)
                sl_p       = float(pos.get("sl") or 0) or None
                lots_str   = f"{float(pos.get('volume') or 0):.2f}"
                lots_col   = "#e5e7eb"

            dir_col = _BULL_COL if direction == "BUY" else _BEAR_COL
            p_col   = _pnl_col(profit)
            node_lbl = "Remote" if remote else _untracked_position_node_label()

            elapsed = ""
            try:
                _ot = (remote.get("open_time") if remote else None) or pos.get("open_time") or 0
                secs = int(datetime.now(timezone.utc).timestamp() - float(_ot))
                elapsed = (f"{secs // 60}m {secs % 60}s"
                           if secs < 3600 else
                           f"{secs // 3600}h {(secs % 3600) // 60}m")
            except Exception:
                pass

            with ui.card().classes("w-full bg-gray-800 rounded-lg p-0 overflow-hidden mb-2"):
                with ui.row().classes(
                    "w-full px-4 py-2 items-center gap-3"
                ).style("background:#1a2236; border-bottom:1px solid #1f2937"):
                    ui.element("div").classes("w-1 h-5 rounded shrink-0").style(
                        f"background:{dir_col}"
                    )
                    ui.label(f"{direction} XAUUSD").classes(
                        "text-sm font-bold"
                    ).style(f"color:{dir_col}")
                    ui.label(f"#{ticket}").classes("text-xs text-gray-500")
                    if strat_name:
                        ui.label(strat_name).classes("text-xs text-gray-500")
                    if remote:
                        src_lbl = trade_source_label(remote.get("tg_source", ""))
                        ui.badge(src_lbl, color="purple").classes("text-xs")
                        ch = trade_channel_label(remote.get("tg_source", ""))
                        if ch and ch != src_lbl:
                            ui.badge(ch, color="indigo").classes("text-xs")
                    ui.space()
                    ui.badge(node_lbl, color="blue" if remote else "purple").classes("text-xs")
                    if remote and remote.get("sl_moved_to_be"):
                        ui.label("Breakeven").classes(
                            "text-xs px-2 py-0.5 rounded font-bold text-green-400"
                        ).style("background:rgba(34,197,94,0.12)").tooltip(
                            "Stop Loss has been moved to entry price — "
                            "this trade cannot lose money from the original risk."
                        )
                    ui.label(elapsed).classes("text-xs text-gray-500")

                with ui.grid(columns=6).classes("w-full px-4 py-3 gap-x-4 gap-y-1"):
                    for label, val, col in [
                        ("ENTRY",      f"${entry_p:.2f}",                       "#e5e7eb"),
                        ("CURRENT",    f"${current_p:.2f}" if current_p else "—", "#e5e7eb"),
                        ("LOTS",       lots_str,                                lots_col),
                        ("SL",         f"${sl_p:.2f}" if sl_p else "—",         "#f87171"),
                        ("UNREAL P&L", f"${profit:+.2f}",                       p_col),
                        ("TPs",        "",                                      "#4ade80"),
                    ]:
                        with ui.column().classes("gap-0"):
                            ui.label(label).classes(
                                "text-xs text-gray-500 tracking-wider"
                            )
                            if label == "TPs":
                                with ui.row().classes("gap-1 flex-wrap"):
                                    if remote:
                                        has_any_tp = False
                                        for n in range(1, 9):
                                            tp_val = remote.get(f"tp{n}")
                                            if not tp_val:
                                                continue
                                            has_any_tp = True
                                            hit = n in triggered
                                            bg_col = "#22c55e" if hit else "#374151"
                                            ui.label(f"TP{n}").classes(
                                                "text-xs px-1.5 py-0.5 rounded "
                                                "font-semibold text-white"
                                            ).style(f"background:{bg_col}").tooltip(f"${float(tp_val):.2f}")
                                        if not has_any_tp:
                                            ui.label("—").classes("text-sm font-mono").style(f"color:{col}")
                                    else:
                                        tp_p = float(pos.get("tp") or 0) or None
                                        if tp_p:
                                            ui.label(f"{tp_p:.2f}").classes(
                                                "text-xs px-1.5 py-0.5 rounded "
                                                "font-semibold text-white"
                                            ).style("background:#374151")
                                        else:
                                            ui.label("—").classes("text-sm font-mono").style(f"color:{col}")
                            else:
                                ui.label(val).classes(
                                    "text-sm font-mono font-semibold"
                                ).style(f"color:{col}")

        rs_chart   = chart_controller.get_risk_settings()
        dpm_active = bool(rs_chart.get("dpm_enabled", 0))
        for t in trades:
            direction = t.get("direction", "?")
            entry_p   = float(t["entry_price"])
            lot_size  = float(t["lot_size"])
            rem_lots  = float(t["remaining_lots"])
            sl        = float(t["stop_loss"]) if t.get("stop_loss") else None
            strategy  = "DPM" if dpm_active else STRATEGY_NAMES.get(t.get("strategy", ""), "")
            ticket    = t.get("mt5_ticket", "—")
            open_time = t.get("open_time", 0)

            current = unreal = 0.0
            if tick:
                current = tick.bid if direction == "BUY" else tick.ask
                unreal  = engine.pnl(direction, entry_p, current, rem_lots)

            # Duration
            elapsed = ""
            try:
                secs = int(datetime.now(timezone.utc).timestamp() - float(open_time))
                elapsed = (f"{secs // 60}m {secs % 60}s"
                           if secs < 3600 else
                           f"{secs // 3600}h {(secs % 3600) // 60}m")
            except Exception:
                pass

            triggered = await engine.get_triggered_tps(t["trade_id"])
            dir_col   = _BULL_COL if direction == "BUY" else _BEAR_COL
            p_col     = _pnl_col(unreal)

            with ui.card().classes(
                "w-full bg-gray-800 rounded-lg p-0 overflow-hidden"
            ):
                with ui.row().classes(
                    "w-full px-4 py-2 items-center gap-3"
                ).style("background:#1a2236; border-bottom:1px solid #1f2937"):
                    ui.element("div").classes("w-1 h-5 rounded shrink-0").style(
                        f"background:{dir_col}"
                    )
                    ui.label(f"{direction} XAUUSD").classes(
                        "text-sm font-bold"
                    ).style(f"color:{dir_col}")
                    ui.label(f"#{ticket}").classes("text-xs text-gray-500")
                    ui.label(strategy).classes("text-xs text-gray-500")
                    src_lbl = trade_source_label(t.get("tg_source", ""))
                    ui.badge(src_lbl, color="purple").classes("text-xs")
                    ch = trade_channel_label(t.get("tg_source", ""))
                    if ch and ch != src_lbl:
                        ui.badge(ch, color="indigo").classes("text-xs")
                    ui.space()
                    if t.get("sl_moved_to_be"):
                        ui.label("Breakeven").classes(
                            "text-xs px-2 py-0.5 rounded font-bold text-green-400"
                        ).style("background:rgba(34,197,94,0.12)").tooltip(
                            "Stop Loss has been moved to entry price — "
                            "this trade cannot lose money from the original risk."
                        )
                    ui.label(elapsed).classes("text-xs text-gray-500")

                closed_lots = round(lot_size - rem_lots, 4)
                lots_str = (
                    f"{lot_size:.2f} → {rem_lots:.2f}"
                    if closed_lots > 0
                    else f"{lot_size:.2f}"
                )
                lots_col = "#fb923c" if closed_lots > 0 else "#e5e7eb"
                with ui.grid(columns=6).classes("w-full px-4 py-3 gap-x-4 gap-y-1"):
                    for label, val, col in [
                        ("ENTRY",      f"${entry_p:.2f}",                     "#e5e7eb"),
                        ("CURRENT",    f"${current:.2f}" if current else "—", "#e5e7eb"),
                        ("LOTS",       lots_str,                              lots_col),
                        ("SL",         f"${sl:.2f}" if sl else "—",          "#f87171"),
                        ("UNREAL P&L", f"${unreal:+.2f}",                    p_col),
                        ("TPs",        "",                                    "#4ade80"),
                    ]:
                        with ui.column().classes("gap-0"):
                            ui.label(label).classes(
                                "text-xs text-gray-500 tracking-wider"
                            )
                            if label == "TPs":
                                with ui.row().classes("gap-1 flex-wrap"):
                                    for n in range(1, 6):
                                        if not t.get(f"tp{n}"):
                                            continue
                                        hit    = n in triggered
                                        bg_col = "#22c55e" if hit else "#374151"
                                        ui.label(f"TP{n}").classes(
                                            "text-xs px-1.5 py-0.5 rounded "
                                            "font-semibold text-white"
                                        ).style(f"background:{bg_col}")
                            else:
                                ui.label(val).classes(
                                    "text-sm font-mono font-semibold"
                                ).style(f"color:{col}")

                with ui.row().classes(
                    "w-full px-4 py-2 gap-2 items-center"
                ).style("border-top:1px solid #1f2937"):
                    trade_id = t["trade_id"]

                    async def do_close(tid=trade_id):
                        try:
                            await engine.close_trade(tid, "manual_close")
                            ui.notify("Trade closed", type="positive")
                        except Exception as e:
                            ui.notify(str(e), type="negative")

                    ui.button("Close Trade", on_click=do_close).style(
                        "background:#7f1d1d; color:#fff; "
                        "font-size:11px; padding:3px 12px"
                    )
