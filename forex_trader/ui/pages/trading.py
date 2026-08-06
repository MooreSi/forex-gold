"""Trading page — active positions, signal entry, strategy, Telegram signals."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

from nicegui import ui

from forex_trader.core import ai_provider
from forex_trader.core import database as db_module
from forex_trader.core.core_trade_reporting import is_stuck_placeholder
from forex_trader.core.models import (
    STRATEGY_NAMES, STRATEGY_SCALE_OUT, STRATEGY_ORB_FIXED,
)
from forex_trader.core.signal_parser import validate_signal
from forex_trader.sync import client as sync_client
from forex_trader.sync.remote_stats_facade import _is_remote_active
from forex_trader.ui.pages.settings import render_risk_card

def _uk(ts) -> str:
    """Format an MT5 broker timestamp for display.
    MT5 timestamps are UTC+3 encoded as Unix epoch; treating as UTC gives broker time."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        # ISO string (e.g. from Telegram message_ts)
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M")
        except Exception:
            return str(ts)[:16]
    except Exception:
        return str(ts)[:16]


def trade_source_label(tg_source: str) -> str:
    """Return a short human-readable label for where a trade originated.

    tg_source values:
      - "manual_market"  → placed via Market Order button
      - "MT5_imported"   → position imported from MT5 sync
      - channel name     → Telegram signal (auto-executed, activated, or IME)
      - "" / None        → manually created signal via New Signal form
    """
    if not tg_source:
        return "Manual Signal"
    if tg_source == "manual_market":
        return "Manual Market"
    if tg_source == "MT5_imported":
        return "MT5 Import"
    # Strip legacy "instant:" prefix stored in older DB records
    if tg_source.startswith("instant:"):
        tg_source = tg_source[len("instant:"):]
    return tg_source


def trade_channel_label(tg_source: str) -> str:
    """Return the Telegram channel name, or empty string if not a Telegram signal."""
    if not tg_source or tg_source in ("manual_market", "MT5_imported", "Signal Generator", "Bounce Generator"):
        return ""
    if tg_source.startswith("instant:"):
        return tg_source[len("instant:"):]
    return tg_source


def _pnl_colour(v: float) -> str:
    if v > 0:  return "text-green-400"
    if v < 0:  return "text-red-400"
    return "text-gray-400"


def _pnl_bg(v: float) -> str:
    if v > 0:  return "bg-green-900"
    if v < 0:  return "bg-red-900"
    return "bg-gray-800"


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
        except Exception:
            pass
        # Circuit breaker badge (right-aligned)
        try:
            _cb = db_module.get_circuit_breaker_state()
            if _cb["is_active"]:
                _rem = int(_cb["remaining_secs"])
                _hms = f"{_rem // 3600:02d}:{(_rem % 3600) // 60:02d}:{_rem % 60:02d}"
                cb_lbl.text = f"CIRCUIT BREAKER ACTIVE — resumes in {_hms}"
                cb_lbl.set_visibility(True)
            else:
                cb_lbl.set_visibility(False)
        except Exception:
            pass

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

def _tp_progress(triggered: set[int], trade: dict) -> None:
    """Render TP1–TP5 chips with actual price values."""
    with ui.column().classes("gap-0.5"):
        for n in range(1, 9):
            tp_val = trade.get(f"tp{n}")
            if not tp_val:
                continue
            hit = n in triggered
            chip_col = "bg-green-500" if hit else "bg-gray-600"
            val_col  = "text-green-300" if hit else "text-gray-400"
            with ui.row().classes("items-center gap-1"):
                ui.label(f"TP{n}").classes(
                    f"text-xs px-1.5 py-0.5 rounded font-semibold text-white {chip_col}"
                )
                ui.label(f"${float(tp_val):.2f}").classes(
                    f"text-xs font-mono {val_col}"
                )


def _stat_cell(label: str, value: str, value_cls: str = "text-gray-200") -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs text-gray-500 tracking-wider font-medium")
        ui.label(value).classes(f"text-sm font-mono font-semibold {value_cls}")


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


# ── Active trades ──────────────────────────────────────────────────────────────

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
            trades     = await db_module.to_db_thread(engine.get_open_trades)
            trades     = [t for t in trades if not is_stuck_placeholder(t)]
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

            rs         = db_module.get_risk_settings()
            dpm_active = bool(rs.get("dpm_enabled", 0))
            _, ooh_now = db_module.get_effective_strategy(rs)
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

                triggered = await engine._get_triggered_tps(trade_id)

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
                            rs_now = db_module.get_risk_settings()
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
                        dpm_on = bool(db_module.get_risk_settings().get("dpm_enabled", 0))
                        if dpm_on:
                            dpm_status = db_module.get_app_config(
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
                                        await engine._sync_profit(tid, int(trow["mt5_ticket"]))
                                        ui.notify("Synced with MT5", type="positive")
                                        await refresh()
                                except Exception as e:
                                    ui.notify(str(e), type="negative")

                            ui.button("Sync MT5", on_click=do_sync).classes(
                                "text-blue-400 text-xs underline bg-transparent px-2 py-1"
                            )


    ui.timer(5.0, refresh)
    asyncio.ensure_future(refresh())


# ── Pending signals ────────────────────────────────────────────────────────────

def _render_pending_signals(engine):
    container = ui.column().classes("w-full gap-3")

    async def refresh():
        container.clear()
        sigs = await db_module.to_db_thread(engine.get_signals, status="pending")
        with container:
            if not sigs:
                ui.label(
                    "No pending signals. Use 'Limit Order' to create one, or signals from "
                    "Telegram that haven't been executed will appear here."
                ).classes("text-gray-500 text-sm italic p-4")
                return

            for s in sigs:
                signal_id = s["signal_id"]
                direction = s.get("direction", "?")
                source    = s.get("source_name", "Manual")

                btn_ref = [None]

                async def open_trade(sid=signal_id, _btn=btn_ref):
                    if _btn[0]:
                        _btn[0].props("loading=true disabled=true")
                    try:
                        result = await engine.open_trade_from_signal(sid)
                        ui.notify(
                            f"Trade opened @ {result['entry_price']}", type="positive"
                        )
                        await refresh()
                    except Exception as e:
                        ui.notify(str(e), type="negative")
                    finally:
                        if _btn[0]:
                            try:
                                _btn[0].props(remove="loading disabled")
                            except Exception:
                                pass

                def cancel_sig(sid=signal_id):
                    engine.cancel_signal(sid)
                    ui.notify("Signal cancelled", type="warning")
                    asyncio.create_task(refresh())

                with ui.card().classes("w-full max-w-2xl bg-gray-800 p-4 rounded-lg"):
                    with ui.row().classes("items-center justify-between mb-2"):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(direction, color="green" if direction == "BUY" else "red")
                            ui.label(f"XAUUSD — {source}").classes(
                                "text-sm font-semibold text-gray-200"
                            )
                        ui.label(_uk(s.get("created_at"))).classes("text-xs text-gray-500")

                    with ui.grid(columns=4).classes("w-full text-sm gap-2"):
                        _stat_cell(
                            "ENTRY RANGE",
                            f"{float(s['entry_low']):.2f} – {float(s['entry_high']):.2f}",
                        )
                        _stat_cell("SL", f"{float(s['stop_loss']):.2f}", "text-red-400")
                        tp_str = "  ".join(
                            f"TP{i}: {float(s[f'tp{i}']):.0f}"
                            for i in range(1, 9) if s.get(f"tp{i}")
                        )
                        _stat_cell("TPs", tp_str or "—", "text-green-400")
                        _stat_cell("SIGNAL ID", signal_id[:8])

                    if s.get("notes"):
                        ui.label(s["notes"]).classes("text-xs text-gray-500 mt-1")

                    # Claude commentary if available
                    commentary = s.get("claude_commentary")
                    if commentary and isinstance(commentary, dict) and commentary.get("summary"):
                        with ui.expansion("AI Commentary", icon="smart_toy").classes(
                            "w-full bg-gray-700 rounded mt-2 text-xs"
                        ):
                            ui.label(commentary["summary"]).classes("text-gray-300 text-xs p-2")

                    with ui.row().classes("gap-2 mt-3 flex-wrap"):
                        btn_ref[0] = ui.button("Open Trade Now", on_click=open_trade).classes(
                            "bg-green-700 text-white text-xs px-3 py-1"
                        )

                        # ── Edit signal dialog ─────────────────────────────────
                        with ui.dialog() as edit_dialog, ui.card().classes(
                            "bg-gray-800 p-5 rounded-lg w-full max-w-lg"
                        ):
                            ui.label("Edit Signal").classes(
                                "text-base font-semibold text-yellow-300 mb-3"
                            )
                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "BUY = expecting price to rise (long). SELL = expecting price to fall (short)."
                                    )
                                e_dir = ui.select(
                                    ["BUY", "SELL"], value=s.get("direction", "BUY"),
                                ).classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Entry Low").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "The lower price of your entry zone. For a single entry price, "
                                        "set both Low and High to the same value."
                                    )
                                e_el = ui.number(value=float(s.get("entry_low", 0)), format="%.2f").classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Entry High").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "The upper price of your entry zone. Must be >= Entry Low. "
                                        "For a single entry price, set both Low and High to the same value."
                                    )
                                e_eh = ui.number(value=float(s.get("entry_high", 0)), format="%.2f").classes("w-full")

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Stop Loss").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "Price at which the trade closes at a loss. "
                                        "For BUY: place below entry. For SELL: place above entry."
                                    )
                                e_sl = ui.number(value=float(s.get("stop_loss", 0)), format="%.2f").classes("w-full")

                            with ui.expansion("Take Profit Levels", icon="expand_more").classes(
                                "w-full bg-gray-700 rounded mt-3"
                            ):
                                with ui.grid(columns=3).classes("gap-3 p-1"):
                                    _etp_defs = [
                                        ("TP1", "First target. Most strategies close a portion here and move SL to breakeven."),
                                        ("TP2", "Second target. Remaining position continues after TP1 is hit."),
                                        ("TP3", "Third target."),
                                        ("TP4", "Fourth target."),
                                        ("TP5", "Fifth target."),
                                        ("TP6", "Sixth target."),
                                        ("TP7", "Seventh target."),
                                        ("TP8", "Final target — Conservative strategy exits at TP7 (second-to-last); TP8 is headroom only."),
                                    ]
                                    _etp_keys = ["tp1","tp2","tp3","tp4","tp5","tp6","tp7","tp8"]
                                    _etp_inputs = []
                                    for (_elbl, _etip), _ekey in zip(_etp_defs, _etp_keys):
                                        with ui.column().classes("gap-0"):
                                            with ui.row().classes("items-center gap-0.5"):
                                                ui.label(_elbl).classes("text-xs text-gray-400 font-medium")
                                                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(_etip)
                                            _etp_inputs.append(
                                                ui.number(value=float(s.get(_ekey) or 0), format="%.2f").classes("w-full")
                                            )
                                    e_tp1, e_tp2, e_tp3, e_tp4, e_tp5, e_tp6, e_tp7, e_tp8 = _etp_inputs

                            with ui.column().classes("gap-0 w-full"):
                                with ui.row().classes("items-center gap-1"):
                                    ui.label("Notes (optional)").classes("text-xs text-gray-400 font-medium")
                                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                                        "Free text notes about this signal — e.g. source channel, setup reason."
                                    )
                                e_notes = ui.input(value=s.get("notes", "") or "").classes("w-full")
                            e_result = ui.label("").classes("text-xs text-gray-400 mt-1")

                            async def save_edit(sid=signal_id):
                                try:
                                    updates = {
                                        "direction":  e_dir.value,
                                        "entry_low":  float(e_el.value or 0),
                                        "entry_high": float(e_eh.value or 0),
                                        "stop_loss":  float(e_sl.value or 0),
                                        "tp1": float(e_tp1.value) if e_tp1.value else None,
                                        "tp2": float(e_tp2.value) if e_tp2.value else None,
                                        "tp3": float(e_tp3.value) if e_tp3.value else None,
                                        "tp4": float(e_tp4.value) if e_tp4.value else None,
                                        "tp5": float(e_tp5.value) if e_tp5.value else None,
                                        "tp6": float(e_tp6.value) if e_tp6.value else None,
                                        "tp7": float(e_tp7.value) if e_tp7.value else None,
                                        "tp8": float(e_tp8.value) if e_tp8.value else None,
                                        "notes": e_notes.value or "",
                                    }
                                    result = await engine.update_signal(sid, updates)
                                    trade_note = ""
                                    if result.get("trade_updated"):
                                        trade_note = " — open trade SL/TP updated"
                                    e_result.text = f"Saved{trade_note}"
                                    e_result.classes(replace="text-xs text-green-400 mt-1")
                                    ui.notify(f"Signal updated{trade_note}", type="positive")
                                    edit_dialog.close()
                                    await refresh()
                                except Exception as ex:
                                    e_result.text = str(ex)
                                    e_result.classes(replace="text-xs text-red-400 mt-1")
                                    ui.notify(str(ex), type="negative")

                            with ui.row().classes("gap-2 mt-3"):
                                ui.button(
                                    "Save Changes",
                                    on_click=lambda: asyncio.create_task(save_edit()),
                                ).classes("bg-blue-700 text-white px-4 py-2")
                                ui.button("Cancel", on_click=edit_dialog.close).classes(
                                    "bg-gray-700 text-white px-4 py-2"
                                )

                        ui.button(
                            "Edit Signal", icon="edit", on_click=edit_dialog.open
                        ).classes("bg-gray-700 text-white text-xs px-3 py-1")

                        ui.button("Cancel Signal", on_click=cancel_sig).classes(
                            "bg-gray-600 text-white text-xs px-3 py-1"
                        )

    ui.timer(5.0, refresh)
    asyncio.ensure_future(refresh())


# ── Manual signal entry ────────────────────────────────────────────────────────

def _render_signal_entry(engine):
    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Create Limit Order").classes("text-lg font-bold text-yellow-300 mb-4")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "BUY = expecting price to rise (long). SELL = expecting price to fall (short)."
                )
            direction = ui.select(["BUY", "SELL"], value="BUY").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Entry Low").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The lower price of your entry zone. The trade opens when current price is within "
                    "this range. For a single entry price, set both Low and High to the same value."
                )
            entry_low = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Entry High").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The upper price of your entry zone. Must be >= Entry Low. "
                    "For a single entry price, set both Low and High to the same value."
                )
            entry_high = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Stop Loss").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The price at which the trade is automatically closed at a loss to protect your "
                    "account. For BUY trades: place below entry. For SELL trades: place above entry."
                )
            stop_loss = ui.number(value=0.0, format="%.2f").classes("w-full")

        with ui.expansion("Take Profit Levels", icon="add").classes("w-full bg-gray-700 rounded mt-3"):
            ui.label(
                "TP levels are checked in order. The strategy determines what happens at each "
                "level (partial close, SL move, etc). At minimum set TP1."
            ).classes("text-xs text-gray-500 p-2")
            with ui.grid(columns=3).classes("gap-3 p-1"):
                _tp_defs = [
                    ("TP1", "First target. Most strategies close a portion here and move SL to breakeven."),
                    ("TP2", "Second target. Remaining position continues after TP1 is hit."),
                    ("TP3", "Third target. Conservative strategy skips this level."),
                    ("TP4", "Fourth target. Conservative strategy closes 10% here and steps up SL."),
                    ("TP5", "Fifth target."),
                    ("TP6", "Sixth target."),
                    ("TP7", "Seventh target."),
                    ("TP8", "Final target — Conservative strategy exits at TP7 (second-to-last); TP8 is headroom only."),
                ]
                _tp_inputs = []
                for _label, _tip in _tp_defs:
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-0.5"):
                            ui.label(_label).classes("text-xs text-gray-400 font-medium")
                            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(_tip)
                        _tp_inputs.append(ui.number(value=None, format="%.2f").classes("w-full"))
                tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8 = _tp_inputs

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Lot Size").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Set to 0 to auto-calculate from your Risk % setting and the entry-to-SL distance. "
                    "Enter a fixed value (e.g. 0.05) to override automatic sizing."
                )
            lot_size = ui.number(value=0.0, min=0.0, step=0.01, format="%.2f").classes("w-full")

        with ui.column().classes("gap-0 w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Notes (optional)").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Free text notes about this signal — e.g. source channel, setup reason, or context."
                )
            notes = ui.input().classes("w-full")
        status_lbl = ui.label("").classes("text-sm text-red-300 mt-1")

        def _validate() -> list[str]:
            return validate_signal(
                direction.value, entry_low.value or 0, entry_high.value or 0,
                stop_loss.value or 0,
                tp1.value or None, tp2.value or None, tp3.value or None,
                tp4.value or None, tp5.value or None,
                tp6.value or None, tp7.value or None, tp8.value or None,
            )

        async def submit():
            status_lbl.text = ""
            errors = _validate()
            if errors:
                status_lbl.text = " | ".join(errors)
                return
            try:
                sig = engine.create_signal(
                    source_name="Manual", direction=direction.value,
                    entry_low=entry_low.value or 0, entry_high=entry_high.value or 0,
                    stop_loss=stop_loss.value or 0,
                    tp1=tp1.value or None, tp2=tp2.value or None, tp3=tp3.value or None,
                    tp4=tp4.value or None, tp5=tp5.value or None,
                    tp6=tp6.value or None, tp7=tp7.value or None, tp8=tp8.value or None,
                    lot_size=lot_size.value if lot_size.value else None,
                    notes=notes.value,
                )
                ui.notify(
                    f"Signal saved — see Pending Signals tab to execute it.",
                    type="positive",
                )
                asyncio.create_task(_background_commentary(engine, sig["signal_id"]))
            except Exception as e:
                status_lbl.text = str(e)

        async def submit_and_open():
            # Places a genuine broker-side resting BuyLimit/SellLimit via the
            # EA (core_manual_limit_order.py) -- fixed 2026-07-24. Previously
            # routed through open_trade_from_signal(), the same "wait for
            # price to re-enter the zone, fill at MARKET" path the automatic
            # Telegram zone-signal handler uses, which only worked when price
            # already happened to sit inside Entry Low-High at the moment of
            # the click and rejected with "price is above/below the entry
            # zone" otherwise -- backwards for a page whose whole point is to
            # place an order that rests until price gets there.
            status_lbl.text = ""
            errors = _validate()
            if errors:
                status_lbl.text = " | ".join(errors)
                return
            try:
                result = await engine.open_manual_limit_order(
                    direction=direction.value,
                    entry_low=entry_low.value or 0, entry_high=entry_high.value or 0,
                    stop_loss=stop_loss.value or 0,
                    tp1=tp1.value or None, tp2=tp2.value or None, tp3=tp3.value or None,
                    tp4=tp4.value or None, tp5=tp5.value or None,
                    tp6=tp6.value or None, tp7=tp7.value or None, tp8=tp8.value or None,
                    lot_size=lot_size.value if lot_size.value else None,
                    notes=notes.value,
                )
                ui.notify(
                    f"Limit order placed @ {result['price']:.2f} — EA ticket {result['mt5_ticket']}",
                    type="positive",
                )
            except Exception as e:
                status_lbl.text = str(e)
                ui.notify(str(e), type="negative")

        with ui.row().classes("gap-2 mt-4"):
            ui.button("Save Signal", on_click=submit).classes("bg-blue-700 text-white px-4 py-2")
            ui.button("Place Limit Order", on_click=submit_and_open).classes(
                "bg-green-700 text-white px-4 py-2"
            )

    ui.label(
        "Save Signal records a zone-watch signal (Pending Signals tab) that fills at market once "
        "price returns to the zone. Place Limit Order sends a genuine resting BuyLimit/SellLimit "
        "order to the broker via the EA immediately — requires the EA bridge connected and healthy."
    ).classes("text-xs text-gray-500 mt-2 max-w-2xl")


def _render_market_order_form(engine):
    """Immediate market entry: choose direction, optional SL/lot override, hit the button."""

    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        # ── Header ────────────────────────────────────────────────────────────
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.icon("bolt").classes("text-amber-400 text-xl")
            ui.label("Market Order").classes("text-lg font-bold text-yellow-300")

        # Live price display
        price_label = ui.label("Current price: fetching...").classes(
            "text-sm text-gray-300 bg-gray-700 px-3 py-1 rounded mb-4 w-fit"
        )

        # Strategy / DPM status banner
        rs_now   = db_module.get_risk_settings()
        dpm_on   = bool(rs_now.get("dpm_enabled", 0))
        strat_nm = STRATEGY_NAMES.get(rs_now.get("trade_strategy", ""), "Scale Out")
        mode_txt = "DPM" if dpm_on else strat_nm
        mode_col = "text-blue-300" if dpm_on else "text-amber-300"
        mode_lbl = ui.label(f"Active mode: {mode_txt}").classes(
            f"text-xs font-semibold {mode_col} mb-3"
        )

        # ── Direction ─────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Direction").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "BUY = go long (expecting price to rise). SELL = go short (expecting price to fall)."
                )
            mo_direction = ui.select(["BUY", "SELL"], value="BUY").classes("w-full")

        # ── Stop Loss ─────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Stop Loss (optional when DPM is ON)").classes(
                    "text-xs text-gray-400 font-medium"
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "The price at which the trade will be closed at a loss. "
                    "If left at 0 and DPM is enabled, an ATR-based stop is set automatically. "
                    "Required when DPM is OFF."
                )
            mo_sl = ui.number(
                value=0.0, min=0.0, step=0.5, format="%.2f",
                placeholder="0 = auto (DPM only)",
            ).classes("w-full")

        # ── Lot Size ──────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-2"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Lot Size (optional)").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Leave at 0 to auto-size from your Risk % and the stop distance. "
                    "Enter a fixed value to override."
                )
            mo_lot = ui.number(
                value=0.0, min=0.0, max=10.0, step=0.01, format="%.2f",
                placeholder="0 = auto",
            ).classes("w-full")

        # ── Strategy ──────────────────────────────────────────────────────────
        with ui.column().classes("gap-0 w-full mb-4"):
            with ui.row().classes("items-center gap-1"):
                ui.label("Strategy").classes("text-xs text-gray-400 font-medium")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Strategy applied to this manual market order. "
                    "Overrides the global Active Strategy for this trade only."
                )
            _mo_strat_names = {
                k: v for k, v in STRATEGY_NAMES.items()
                if k != STRATEGY_ORB_FIXED
            }
            _mo_rs = db_module.get_risk_settings()
            _mo_default = _mo_rs.get("trade_strategy", STRATEGY_SCALE_OUT)
            mo_strategy = ui.select(
                _mo_strat_names,
                value=_mo_default if _mo_default in _mo_strat_names else STRATEGY_SCALE_OUT,
            ).classes("w-full")

        status_lbl = ui.label("").classes("text-sm text-red-300 mb-2")

        async def _enter_market():
            status_lbl.text = ""
            # "disabled" is not a real Quasar QBtn prop (it's "disable") and,
            # even if it were, HTML's disabled attribute disables on mere
            # presence regardless of its string value — so a later
            # .props("disabled=false") never actually re-enabled the button.
            # .disable()/.enable() set the real boolean prop correctly.
            btn.props("loading=true")
            btn.disable()
            try:
                sl_val  = float(mo_sl.value  or 0)
                lot_val = float(mo_lot.value or 0)
                _sl     = sl_val  if sl_val  > 0 else None
                _lot    = lot_val if lot_val > 0 else None
                _strat  = mo_strategy.value or None

                if _is_remote_active():
                    # This node is stood down (VPS is the active trader) — route
                    # the order to the VPS's own account instead of just
                    # failing with "Trading stood down". Mirrors the Signal
                    # Generator panels' remote Start/Stop/Run Now pattern.
                    cli = sync_client.get_instance()
                    if cli is None:
                        raise ConnectionError("Not connected to VPS")
                    ack = await cli.send_market_order(
                        direction=mo_direction.value,
                        stop_loss=_sl, lot_size=_lot, strategy=_strat,
                    )
                    if ack.get("error"):
                        raise RuntimeError(ack["error"])
                    result = ack.get("result") or {}
                else:
                    result = await engine.open_manual_market_order(
                        direction=mo_direction.value,
                        stop_loss=_sl,
                        lot_size=_lot,
                        strategy=_strat,
                    )
                entry = float(result.get("entry_price", 0))
                ticket = result.get("mt5_ticket", "—")
                ui.notify(
                    f"{mo_direction.value} opened @ {entry:.2f}  |  ticket {ticket}",
                    type="positive",
                )
            except Exception as exc:
                status_lbl.text = str(exc)
                ui.notify(str(exc), type="negative")
            finally:
                btn.props("loading=false")
                btn.enable()

        btn = ui.button(
            "Enter at Market Price",
            on_click=_enter_market,
        ).classes("bg-amber-600 hover:bg-amber-500 text-white w-full py-3 text-base font-semibold mt-2")

        # Refresh live price and active mode on a short timer
        async def _refresh_price():
            try:
                tick = await engine.get_tick()
                if tick:
                    bid, ask = tick.bid, tick.ask
                    price_label.text = (
                        f"Current price:  Bid {bid:.2f}  |  Ask {ask:.2f}  "
                        f"|  Spread {tick.spread_points:.0f} pts"
                    )
                rs_r  = await db_module.to_db_thread(db_module.get_risk_settings)
                d_on  = bool(rs_r.get("dpm_enabled", 0))
                s_nm  = STRATEGY_NAMES.get(rs_r.get("trade_strategy", ""), "Scale Out")
                m_txt = "DPM" if d_on else s_nm
                m_col = "text-blue-300 text-xs font-semibold mb-3" if d_on \
                        else "text-amber-300 text-xs font-semibold mb-3"
                mode_lbl.text = f"Active mode: {m_txt}"
                mode_lbl.classes(replace=m_col)
            except Exception:
                pass

        ui.timer(3.0, _refresh_price)

    ui.label(
        "Places an order immediately at the current market price using your active "
        "strategy and risk settings. With DPM enabled, a stop is auto-calculated from ATR "
        "and DPM manages the trade from there."
    ).classes("text-xs text-gray-500 mt-2 max-w-2xl")


def _render_orb_report(engine):
    """London opening-range-breakout report — classic ORB methodology
    (https://www.litefinance.org/blog/for-beginners/trading-strategies/opening-range-breakout-strategy/):
    the whole Asian session (00:00-08:00 UTC) is a confirmation filter, the
    first 15 minutes of London (08:00-08:15 UTC) is the traded opening
    range, a breakout only counts once price clears BOTH in the same
    direction. Stop at the opening range's midpoint, target at 2x the
    resulting risk (auto-executed as a genuine market order once
    confirmed) with an informational-only 3x level shown alongside it."""
    import base64
    from forex_trader.core import email_service

    ui.button("Refresh", icon="refresh", on_click=lambda: refresh()).props("flat").classes(
        "text-gray-400 mb-2"
    )
    container = ui.column().classes("w-full gap-3")

    async def refresh():
        container.clear()
        try:
            report = await engine.build_orb_report()
        except Exception as e:
            with container:
                ui.label(f"Failed to build ORB report: {e}").classes("text-red-400 text-sm p-4")
            return

        with container:
            if not report:
                ui.label(
                    "No ORB report available yet — this builds from the whole Asian "
                    "session plus the first 15 minutes of London, and is only "
                    "available from London open onward."
                ).classes("text-gray-500 text-sm italic p-4")
                return

            direction = report.get("direction", "inside")
            _label = {"bullish": ("BREAKOUT — BULLISH", "text-green-400"),
                      "bearish": ("BREAKOUT — BEARISH", "text-red-400"),
                      "unconfirmed": ("BROKE OPENING RANGE — UNCONFIRMED", "text-amber-400"),
                      "inside": ("INSIDE RANGE", "text-gray-400")}
            status_txt, status_col = _label.get(direction, ("—", "text-gray-400"))

            with ui.card().classes("w-full max-w-3xl bg-gray-800 p-6 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.icon("candlestick_chart").classes("text-amber-400 text-xl")
                    ui.label("London Open — ORB Report").classes("text-lg font-bold text-yellow-300")

                with ui.row().classes("items-center gap-4 mb-3"):
                    ui.label(f"Current price: ${float(report.get('current_price', 0) or 0):.2f}").classes(
                        "text-sm text-gray-300 bg-gray-700 px-3 py-1 rounded"
                    )
                    ui.label(status_txt).classes(f"text-sm font-bold {status_col}")

                if report.get("phase") == "forming":
                    ui.label(report.get("position_note", "")).classes("text-sm text-gray-400 italic mb-2")
                    _stat_cell(
                        "ASIAN RANGE (00:00–08:00 UTC)",
                        f"${report['asia_low']:.2f} – ${report['asia_high']:.2f}  "
                        f"({report['asia_range']:.1f} pts)",
                    )
                    return

                # Asian range (filter) + London opening range (traded range)
                with ui.grid(columns=2).classes("w-full text-sm gap-2 mb-2"):
                    _stat_cell(
                        "ASIAN RANGE (00:00–08:00 UTC)",
                        f"${report['asia_low']:.2f} – ${report['asia_high']:.2f}  "
                        f"({report['asia_range']:.1f} pts)",
                    )
                    _stat_cell(
                        "LONDON OPENING RANGE (08:00–08:15 UTC)",
                        f"${report['or_low']:.2f} – ${report['or_high']:.2f}  "
                        f"({report['or_range']:.1f} pts)",
                    )

                if report.get("position_note"):
                    ui.label(report["position_note"]).classes("text-xs text-gray-500 mb-3")

                # Chart
                try:
                    chart_png = email_service.build_orb_chart_image(report)
                except Exception as e:
                    chart_png = None
                    log.warning("ORB chart render failed: %s", e)
                if chart_png:
                    b64 = base64.b64encode(chart_png).decode()
                    ui.image(f"data:image/png;base64,{b64}").classes("w-full rounded mb-3")

                # Breakout entry/exit setup, once confirmed
                if direction in ("bullish", "bearish"):
                    rr = report.get("rr")
                    target2 = report.get("target2")

                    ui.label("Breakout Setup").classes(
                        "text-xs font-semibold text-amber-300 uppercase tracking-wide mt-2 mb-1"
                    )
                    with ui.grid(columns=4).classes("w-full text-sm gap-2 mb-1"):
                        _stat_cell("STOP", f"${report['stop']:.2f}", "text-red-400")
                        _stat_cell("TARGET (2:1)", f"${report['target']:.2f}", "text-green-400")
                        _stat_cell(
                            "TARGET 2 (3:1, info only)",
                            f"${target2:.2f}" if target2 else "—", "text-green-400",
                        )
                        _stat_cell("R:R", f"{rr:.2f}:1" if rr else "—", "text-amber-300")
                    ui.label(
                        "Stop = midpoint of the London opening range. Target = 2x the "
                        "resulting risk (auto-executed). Target 2 = 3x risk, shown for "
                        "reference only — the automated path closes fully at Target, "
                        "it does not manage a partial-close ladder."
                    ).classes("text-xs text-gray-500 mb-3")

                    mt5_direction = "BUY" if direction == "bullish" else "SELL"
                    status_lbl = ui.label("").classes("text-sm text-red-300 mb-2")

                    async def _execute_orb(mt5_direction=mt5_direction, report=report):
                        status_lbl.text = ""
                        exec_btn.props("loading=true")
                        exec_btn.disable()
                        try:
                            _lot_val = float(lot_inp.value or 0)
                            _lot = _lot_val if _lot_val > 0 else None
                            if _is_remote_active():
                                cli = sync_client.get_instance()
                                if cli is None:
                                    raise ConnectionError("Not connected to VPS")
                                ack = await cli.send_market_order(
                                    direction=mt5_direction, lot_size=_lot,
                                    stop_loss=report["stop"], take_profit=report["target"],
                                    strategy=STRATEGY_ORB_FIXED, source_name="ORB/IVB Report",
                                )
                                if ack.get("error"):
                                    raise RuntimeError(ack["error"])
                                result = ack.get("result") or {}
                            else:
                                result = await engine.open_manual_market_order(
                                    direction=mt5_direction, lot_size=_lot,
                                    stop_loss=report["stop"], take_profit=report["target"],
                                    strategy=STRATEGY_ORB_FIXED, source_name="ORB/IVB Report",
                                )
                            entry = float(result.get("entry_price", 0))
                            ticket = result.get("mt5_ticket", "—")
                            ui.notify(
                                f"{mt5_direction} opened @ {entry:.2f}  |  ticket {ticket}",
                                type="positive",
                            )
                        except Exception as exc:
                            status_lbl.text = str(exc)
                            ui.notify(str(exc), type="negative")
                        finally:
                            exec_btn.props("loading=false")
                            exec_btn.enable()

                    exec_btn = ui.button(
                        f"Execute {mt5_direction} at Market — ORB Setup",
                        on_click=_execute_orb,
                    ).classes("bg-amber-600 hover:bg-amber-500 text-white w-full py-3 text-base font-semibold")

                    with ui.row().classes("items-center gap-2 mt-2"):
                        ui.label("Lot size").classes("text-xs text-gray-400 font-medium")
                        _orb_lot_rs = db_module.get_risk_settings()
                        lot_inp = ui.number(
                            value=float(_orb_lot_rs.get("orb_lot_size", 0) or 0),
                            min=0.0, max=10.0, step=0.01, format="%.2f",
                            placeholder="0 = auto-size from risk %",
                        ).classes("w-32")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Lots used for both the manual Execute button above and the "
                            "unattended auto-execute below. Saves automatically. "
                            "0 = auto-size from your Risk % and the stop distance."
                        )

                        def _lot_change(e):
                            db_module.update_risk_settings({"orb_lot_size": float(e.value or 0)})

                        lot_inp.on_value_change(_lot_change)
                elif direction == "unconfirmed":
                    ui.label(
                        "Price broke the London opening range but is still inside the "
                        "Asian range — not confirmed. The manual Execute button appears "
                        "once price also clears the Asian range in the same direction."
                    ).classes("text-xs text-gray-500 italic mb-2")
                else:
                    ui.label(
                        "No breakout yet — the manual Execute button appears once price "
                        "clears both the London opening range and the Asian range."
                    ).classes("text-xs text-gray-500 italic mb-2")

                # Auto-execute toggle
                ui.separator().classes("my-3")
                rs_now = db_module.get_risk_settings()
                auto_val = bool(rs_now.get("orb_auto_execute_enabled", 0))
                with ui.row().classes("items-center gap-2"):
                    auto_chk = ui.checkbox(
                        "Auto-execute this setup every morning (unattended)",
                        value=auto_val,
                    ).classes("text-sm text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When on, the app places this trade automatically at 08:15 UK time "
                        "each weekday using the recommendation above — no manual click needed. "
                        "In Remote mode, whichever node is the active trader executes it; the "
                        "other node stays silent to avoid a double trade. Off by default."
                    )

                    def _auto_toggle(e):
                        db_module.update_risk_settings({"orb_auto_execute_enabled": 1 if e.value else 0})
                        ui.notify(
                            "ORB auto-execute enabled" if e.value else "ORB auto-execute disabled",
                            type="positive" if e.value else "info",
                        )

                    auto_chk.on_value_change(_auto_toggle)

    ui.timer(60.0, refresh)


async def _background_commentary(engine, signal_id: str):
    try:
        import forex_trader.config as cfg_module
        import forex_trader.core.claude_ai as claude_ai
        config = cfg_module.load()
        with db_module.db() as conn:
            row = db_module.row_to_dict(
                conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
            )
        tick    = await engine.get_tick()
        candles = await engine.get_candles("M5", 20)
        commentary = await claude_ai.request_commentary(
            "signal_saved", None, row, tick, candles, config,
        )
        if commentary.get("summary"):
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
                    (json.dumps(commentary), signal_id),
                )
    except Exception as _exc:
        log.warning("Background commentary failed for signal %s: %s", signal_id, _exc)


# ── Strategy comparison data ───────────────────────────────────────────────────

from forex_trader.core import core_strategy_params as _sp
from forex_trader.core.models import (
    STRATEGY_SCALE_OUT as _SO, STRATEGY_BE_RUNNER as _BE,
    STRATEGY_TRAIL_STOP as _TS, STRATEGY_PROTECTED_SCALE as _PS,
    STRATEGY_CONSERVATIVE as _CO, STRATEGY_NO_SL_SCALE as _NSS,
    STRATEGY_CONSERVATIVE_TRIAL as _CT, STRATEGY_SCALP_RUNNER as _SR,
    STRATEGY_SIGNAL_CLIMBER as _SC,
    STRATEGY_REVERSAL_RUNNER as _RVR,
    STRATEGY_ADAPTIVE_RUNNER as _AR,
    STRATEGY_ADAPTIVE_RUNNER_2 as _AR2,
)


# Cells below that embed a live-tunable Strategy Parameters value are
# callables (evaluated fresh on every _draw_compare() render) instead of
# plain strings, so an edit on Trading > Strategy > Strategy Parameters is
# reflected here immediately -- see _render_strategy_params_card().
def _ct_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return (
        f"{p['tp1_pct']:g}% TP1 · {p['tp2_pct']:g}% TP2 · {p['tp3_pct']:g}% TP3 · "
        f"{p['tp4_pct']:g}% TP4 · {p['tp5_pct']:g}% TP5 · rest at TP6"
    )


def _ct_be_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return f"At TP2 (+{p['tp2_pt']:g} pts from fill)"


def _ct_max_upside_cell() -> str:
    p = _sp.get_strategy_params(_CT)
    return f"+{p['tp6_pt']:g} pts from fill price (TP6 fixed target)"


def _so_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_SO)
    return (
        f"{p['tp1_pct']:g}% TP1 · {p['tp2_pct']:g}% TP2 · {p['tp3_pct']:g}% TP3 · "
        f"{p['tp4_pct']:g}% TP4 · rest at last TP"
    )


def _ps_partial_closes_cell() -> str:
    p = _sp.get_strategy_params(_PS)
    return f"Yes — {p['mid_tp_close_pct']:g}% from TP3"


def _sc_be_cell() -> str:
    pos = int(_sp.get_strategy_params(_SC).get("be_at_pos", 1))
    return (
        f"After TP{pos} → entry (BE); after TP{pos + 1}+ → trails to previous TP price"
    )


def _be_filter_cell() -> str:
    thr = _sp.get_strategy_params(_BE)["adx_ranging_threshold"]
    return f"ADX > {thr:g} required — falls back to Scale Out in ranging markets"


def _be_best_market_cell() -> str:
    thr = _sp.get_strategy_params(_BE)["adx_ranging_threshold"]
    return f"Strong trend (ADX > {thr:g})"

_STRAT_ORDER = [_SO, _BE, _TS, _PS, _CO, _NSS, _CT, _SR, _SC, _RVR, _AR, _AR2]
_PROTECTED_STRATS = frozenset({_SO, _BE, _TS, _PS, _CO, _SR, _SC, _RVR, _AR, _AR2})


def _render_schedule():
    """Trading Schedule tab — per-day, per-window profit-target discipline
    cap on AUTOMATED order execution (manual orders are always exempt).
    Signal generation and Telegram ingestion are never affected -- see
    core_trading_schedule.py / core_signal_resolution.py for the gate this
    UI configures. Each window also independently toggles Telegram/Reversal
    Engine/Breakout Engine (2026-07-24) -- e.g. Reversal Engine performs
    well overnight but loses during London/NY, the opposite of the Telegram
    channels, so a single blanket switch isn't enough."""
    from forex_trader.core import core_trading_schedule as sched

    schedule = sched.get_trading_schedule()
    enabled_now = sched.is_trading_schedule_enabled()
    rs = db_module.get_risk_settings()

    # ── Trading Markets card ─────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mb-3"):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.label("Trading Markets").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Controls which trading sessions will accept and execute signals.\n\n"
                "Asia:     21:00–07:00 UTC (overnight / off-peak)\n"
                "London:   07:00–16:00 UTC (includes London/NY overlap)\n"
                "New York: 12:00–21:00 UTC (includes London/NY overlap)\n\n"
                "Signals can still be generated at any time, but they will only "
                "trigger and execute live trades during an enabled session.\n\n"
                "If more than one market is selected, the overlapping hours "
                "(12:00–16:00 UTC) are also active."
            )

        _sess_asia   = bool(rs.get("session_asia_enabled",   1))
        _sess_london = bool(rs.get("session_london_enabled", 1))
        _sess_ny     = bool(rs.get("session_ny_enabled",     1))

        def _compute_session_label() -> tuple[str, str]:
            """Return (label, badge_color) based on clock + enabled sessions."""
            from datetime import datetime, timezone as _tz
            from forex_trader.core.dpm_engine import is_weekly_market_closed
            if is_weekly_market_closed():
                return "Markets Closed", "grey"
            h = datetime.now(_tz.utc).hour
            london_open = 7  <= h < 16
            ny_open     = 12 <= h < 21
            asia_open   = not (london_open or ny_open)  # 21:00-07:00 UTC

            latest = db_module.get_risk_settings()
            asia_en   = bool(latest.get("session_asia_enabled",   1))
            london_en = bool(latest.get("session_london_enabled", 1))
            ny_en     = bool(latest.get("session_ny_enabled",     1))

            if london_open and ny_open:
                if london_en or ny_en:
                    return "Overlap (London + NY)", "blue"
                return "Markets Closed", "grey"
            if london_open:
                if london_en:
                    return "London", "blue"
                return "Markets Closed", "grey"
            if ny_open:
                if ny_en:
                    return "New York", "blue"
                return "Markets Closed", "grey"
            # Asian / off-hours
            if asia_en:
                return "Asia", "blue"
            return "Markets Closed", "grey"

        _init_label, _init_color = _compute_session_label()

        # Session indicator row
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.label("Current session:").classes("text-xs text-gray-400")
            sess_badge = ui.badge(_init_label, color=_init_color).classes("text-xs")
        def _refresh_sess_badge(badge=sess_badge):
            lbl, col = _compute_session_label()
            badge.text = lbl
            badge.props(f"color={col}")
        ui.timer(60, _refresh_sess_badge)

        # Three market toggle buttons
        with ui.row().classes("gap-2 flex-wrap"):

            # Asia
            asia_btn = ui.button(
                "Asia",
                icon="nights_stay",
            ).props(
                f"dense {'color=green' if _sess_asia else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "Asian session: 21:00–07:00 UTC\n"
                "Enables signal execution during overnight / off-peak hours."
            )

            # London
            london_btn = ui.button(
                "London",
                icon="location_city",
            ).props(
                f"dense {'color=green' if _sess_london else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "London session: 07:00–16:00 UTC\n"
                "Includes the London/NY overlap (12:00–16:00 UTC)."
            )

            # New York
            ny_btn = ui.button(
                "New York",
                icon="location_on",
            ).props(
                f"dense {'color=green' if _sess_ny else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "New York session: 12:00–21:00 UTC\n"
                "Includes the London/NY overlap (12:00–16:00 UTC)."
            )

        # Status caption
        _active = []
        if _sess_asia:   _active.append("Asia")
        if _sess_london: _active.append("London")
        if _sess_ny:     _active.append("New York")
        mkt_caption = ui.label(
            f"Active: {', '.join(_active)}" if _active else "No sessions active — all signals blocked"
        ).classes("text-xs text-gray-400 mt-1" if _active else "text-xs text-red-400 mt-1")

        def _toggle_market(key: str, btn, caption=mkt_caption):
            cur = bool(db_module.get_risk_settings().get(key, 1))
            new = not cur
            db_module.update_risk_settings({key: 1 if new else 0})
            if new:
                btn.props("color=green")
                btn.props(remove="flat")
            else:
                btn.props("flat color=grey")
            # Rebuild caption
            latest = db_module.get_risk_settings()
            parts = []
            if latest.get("session_asia_enabled",   1): parts.append("Asia")
            if latest.get("session_london_enabled", 1): parts.append("London")
            if latest.get("session_ny_enabled",     1): parts.append("New York")
            if parts:
                caption.text = f"Active: {', '.join(parts)}"
                caption.classes(replace="text-xs text-gray-400 mt-1")
            else:
                caption.text = "No sessions active — all signals blocked"
                caption.classes(replace="text-xs text-red-400 mt-1")
            # Update session badge immediately
            _refresh_sess_badge()
            sess_nm = {"session_asia_enabled": "Asia", "session_london_enabled": "London",
                       "session_ny_enabled": "New York"}[key]
            ui.notify(
                f"{sess_nm} session {'enabled' if new else 'disabled'}",
                type="positive" if new else "warning",
            )

        asia_btn.on("click",   lambda: _toggle_market("session_asia_enabled",   asia_btn))
        london_btn.on("click", lambda: _toggle_market("session_london_enabled", london_btn))
        ny_btn.on("click",     lambda: _toggle_market("session_ny_enabled",     ny_btn))

    with ui.row().classes("items-center gap-2 mb-1"):
        master_chk = ui.checkbox("Trading Schedule", value=enabled_now).classes(
            "text-cyan-300 font-bold text-lg"
        )
    ui.label(
        "Caps automated order execution per day and per time window, once a window's "
        "profit target is met trading pauses until the next window. Open a window's "
        "Channels panel to independently allow/block each Telegram channel plus "
        "Reversal Engine and Breakout Engine -- unchecking one blocks only that "
        "source's live execution for this window, the others are unaffected. Every "
        "item in the panel has its own Override dropdown, so different channels (or "
        "the two engines) can each run a different strategy or EA template within "
        "the same window, taking priority over that source's own Channel Strategy "
        "pick for as long as the window is active. Signal generation and Telegram "
        "ingestion keep running regardless -- this only blocks/redirects the final "
        "order-placement step, and only for automated (not manual) orders."
    ).classes("text-xs text-gray-500 mb-3")

    with ui.row().classes("items-center gap-2 mb-3"):
        daily_target_input = ui.number(
            "Daily Profit Target $", value=sched.get_daily_profit_target(), min=0, step=1.0,
        ).props("dense outlined").classes("w-48")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Cumulative profit across the WHOLE day, all windows combined. Once "
            "reached, automated trading stops for the rest of the day regardless "
            "of which window/hours would otherwise still be open.\n\n"
            "0 = disabled -- reverts to each window's own Target $ above (and if "
            "every window's target is also 0, there is no profit cap at all)."
        )

    # Strategy/EA-template options for the per-window / per-channel override
    # dropdowns -- same combined-enumeration pattern as
    # _render_channel_strategy_card's strat_opts, minus "Inherit Global"/
    # "Auto (Claude)" (those describe a per-channel fallback that doesn't
    # map onto a time window).
    from forex_trader.core import core_ea_templates as _sched_et
    from forex_trader.core.core_db_channel import get_telegram_channel_names
    _sched_strat_opts = {"": "— No Override —"}
    _sched_strat_opts.update(STRATEGY_NAMES)
    for _t in _sched_et.list_ea_templates():
        _sched_strat_opts[_sched_et.override_for_template(_t["name"])] = f"Template: {_t['name']}"

    _sched_channels = get_telegram_channel_names()
    _ENGINE_LABELS = {"reversal_engine": "Reversal Engine", "breakout_engine": "Breakout Engine"}

    _day_widgets: dict[str, list[dict]] = {}

    def _copy_monday_to_all():
        monday_rows = _day_widgets.get("monday", [])
        if not monday_rows:
            return
        copied = 0
        for day, rows in _day_widgets.items():
            if day == "monday":
                continue
            for src, dst in zip(monday_rows, rows):
                for key in (
                    "enabled", "start", "end", "target",
                    "reversal_engine", "breakout_engine",
                    "reversal_engine_override", "breakout_engine_override",
                    "telegram_default_enabled",
                ):
                    dst[key].value = src[key].value
                for ch, src_cw in src["telegram_channels"].items():
                    dst_cw = dst["telegram_channels"].get(ch)
                    if dst_cw is None:
                        continue
                    dst_cw["enabled"].value = src_cw["enabled"].value
                    dst_cw["strategy_override"].value = src_cw["strategy_override"].value
            copied += 1
        ui.notify(
            f"Copied Monday's schedule to {copied} other days — click Save Schedule to persist",
            type="positive",
        )

    with ui.column().classes("w-full gap-2"):
        for day in sched.DAY_NAMES:
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.label(day.title()).classes("font-bold text-yellow-300 text-sm")
                    if day == "monday":
                        ui.button(
                            "Copy to All", icon="content_copy", on_click=_copy_monday_to_all,
                        ).props("dense flat color=blue size=sm").classes("text-xs").tooltip(
                            "Copy every one of Monday's windows (times, targets, engines, "
                            "channels) to the other 6 days"
                        )
                blocks = schedule[day]
                _day_widgets[day] = []
                for i, block in enumerate(blocks):
                    with ui.column().classes(
                        "w-full gap-1 pb-2 mb-1 border-b border-gray-700"
                    ):
                        with ui.row().classes("items-center gap-2 flex-wrap"):
                            en = ui.checkbox("", value=block["enabled"]).props("dense")
                            ui.label(f"Window {i + 1}").classes("text-xs text-gray-400 w-16")
                            start = ui.input("Start", value=block["start"]).props(
                                "dense outlined"
                            ).classes("w-24").tooltip("24-hour HH:MM")
                            ui.label("to").classes("text-xs text-gray-500")
                            end = ui.input("End", value=block["end"]).props(
                                "dense outlined"
                            ).classes("w-24").tooltip("24-hour HH:MM")
                            target = ui.number(
                                "Target $", value=block["target"], min=0, step=1.0
                            ).props("dense outlined").classes("w-28").tooltip(
                                "0 = no profit cap, only the time window applies"
                            )

                        # ── Channels panel: Telegram channels + the two ────
                        # internal engines, each with its own enable toggle
                        # and its own strategy Override.
                        _tg_cfg = block.get("telegram_channels", {})
                        _tg_default = block.get("telegram_default_enabled", True)
                        _tg_on = sum(
                            1 for ch in _sched_channels
                            if _tg_cfg.get(ch, {}).get("enabled", _tg_default)
                        )
                        _engine_on = sum(1 for k in _ENGINE_LABELS if block.get(k, True))
                        _total_on = _tg_on + _engine_on
                        _total_items = len(_sched_channels) + len(_ENGINE_LABELS)
                        _exp_label = f"Channels ({_total_on}/{_total_items} enabled)"
                        with ui.expansion(_exp_label, icon="tune").classes(
                            "w-full text-xs text-gray-400 bg-gray-900 rounded"
                        ).props("dense"):
                            _chan_widgets: dict[str, dict] = {}
                            with ui.column().classes("w-full gap-1 pl-2 pt-1 pb-1"):
                                default_chk = ui.checkbox(
                                    "New/unlisted channels default to enabled",
                                    value=_tg_default,
                                ).props("dense").classes("text-xs text-gray-500").tooltip(
                                    "Applies to any Telegram channel not explicitly set "
                                    "below -- including one added after this schedule "
                                    "was saved."
                                )
                                if _sched_channels:
                                    ui.separator().classes("my-1 bg-gray-700")
                                for ch in _sched_channels:
                                    _ch_cfg = _tg_cfg.get(ch, {})
                                    with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                        c_chk = ui.checkbox(
                                            ch, value=bool(_ch_cfg.get("enabled", _tg_default)),
                                        ).props("dense").classes("text-xs w-56").tooltip(
                                            f"Allow automated trades from {ch} during this window"
                                        )
                                        c_sel = ui.select(
                                            _sched_strat_opts,
                                            value=_ch_cfg.get("strategy_override") or "",
                                            label="Override",
                                        ).props("dense outlined").classes("w-48").tooltip(
                                            f"Force {ch} onto this strategy or EA template "
                                            "while this window is active, overriding its "
                                            "own Channel Strategy pick. No Override = "
                                            "normal per-channel resolution."
                                        )
                                    _chan_widgets[ch] = {"enabled": c_chk, "strategy_override": c_sel}

                                # Internal signal generators -- same row shape
                                # as a channel, each with its own Override so
                                # Reversal Engine and Breakout Engine can run
                                # different strategies in the same window.
                                ui.separator().classes("my-1 bg-gray-700")
                                ui.label("Internal Signal Generators").classes(
                                    "text-xs text-gray-500 uppercase tracking-wide"
                                )
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    re_chk = ui.checkbox(
                                        "Reversal Engine", value=block["reversal_engine"],
                                    ).props("dense").classes("text-xs w-56").tooltip(
                                        "Allow Reversal Engine live execution during this window"
                                    )
                                    re_ov = ui.select(
                                        _sched_strat_opts,
                                        value=block.get("reversal_engine_override") or "",
                                        label="Override",
                                    ).props("dense outlined").classes("w-48").tooltip(
                                        "Force Reversal Engine onto this strategy or EA "
                                        "template while this window is active, overriding "
                                        "its own Channel Strategy pick. No Override = "
                                        "normal resolution."
                                    )
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    bo_chk = ui.checkbox(
                                        "Breakout Engine", value=block["breakout_engine"],
                                    ).props("dense").classes("text-xs w-56").tooltip(
                                        "Allow Breakout Engine live execution during this window"
                                    )
                                    bo_ov = ui.select(
                                        _sched_strat_opts,
                                        value=block.get("breakout_engine_override") or "",
                                        label="Override",
                                    ).props("dense outlined").classes("w-48").tooltip(
                                        "Force Breakout Engine onto this strategy or EA "
                                        "template while this window is active, overriding "
                                        "its own Channel Strategy pick. No Override = "
                                        "normal resolution."
                                    )

                        _day_widgets[day].append({
                            "enabled": en, "start": start, "end": end, "target": target,
                            "reversal_engine": re_chk, "breakout_engine": bo_chk,
                            "reversal_engine_override": re_ov,
                            "breakout_engine_override": bo_ov,
                            "telegram_default_enabled": default_chk,
                            "telegram_channels": _chan_widgets,
                        })

    def _save():
        try:
            new_schedule = {}
            for day, rows in _day_widgets.items():
                blocks = []
                for w in rows:
                    start_val = str(w["start"].value or "00:00").strip()
                    end_val   = str(w["end"].value or "23:59").strip()
                    sched._parse_hm(start_val)  # validates HH:MM, raises on bad input
                    sched._parse_hm(end_val)

                    def _sel_val(widget):
                        v = widget.value
                        return v.get("value") if isinstance(v, dict) else v

                    _tg_channels = {}
                    for ch, cw in w["telegram_channels"].items():
                        _tg_channels[ch] = {
                            "enabled": bool(cw["enabled"].value),
                            "strategy_override": _sel_val(cw["strategy_override"]) or "",
                        }
                    blocks.append({
                        "enabled": bool(w["enabled"].value),
                        "start":   start_val,
                        "end":     end_val,
                        "target":  float(w["target"].value or 0),
                        "reversal_engine":  bool(w["reversal_engine"].value),
                        "breakout_engine":  bool(w["breakout_engine"].value),
                        "reversal_engine_override": _sel_val(w["reversal_engine_override"]) or "",
                        "breakout_engine_override": _sel_val(w["breakout_engine_override"]) or "",
                        "telegram_default_enabled": bool(w["telegram_default_enabled"].value),
                        "telegram_channels": _tg_channels,
                    })
                new_schedule[day] = blocks
        except Exception as e:
            ui.notify(f"Invalid time — use 24-hour HH:MM (e.g. 09:00): {e}", type="negative")
            return
        sched.set_trading_schedule(new_schedule)
        sched.set_trading_schedule_enabled(bool(master_chk.value))
        sched.set_daily_profit_target(float(daily_target_input.value or 0))
        ui.notify(
            "Trading Schedule saved and enabled" if master_chk.value
            else "Trading Schedule saved (currently disabled — automated orders are not restricted)",
            type="positive" if master_chk.value else "info",
        )

    ui.button("Save Schedule", icon="save", on_click=_save).classes(
        "bg-blue-700 text-white px-4 py-2 mt-2"
    )


def _get_hidden_strategies() -> set:
    raw = db_module.get_app_config("hidden_strategies") or "[]"
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def _hide_builtin_strategy(sid: str) -> None:
    hidden = _get_hidden_strategies()
    hidden.add(sid)
    db_module.set_app_config("hidden_strategies", json.dumps(sorted(hidden)))

_COMPARE_ROWS = [
    ("Partial closes", {
        _SO:  _so_partial_closes_cell,
        _BE:  "No",
        _TS:  "No",
        _PS:  _ps_partial_closes_cell,
        _CO:  "80% at TP1 (+3 pts from fill) · remainder trails via 3-pt stop",
        _NSS: "20% TP1 · 20% TP3 · rest at last TP (max TP8)",
        _CT:  _ct_partial_closes_cell,
        _SR:  "50% at TP1 (+3 pts from fill), SL untouched · remainder trails via 3-pt stop from TP2 (+4 pts)",
        _SC:  "20% TP1 · 15% TP2/3/4 · 20% TP5 · rest at TP6+ (signal's own TPs used)",
        _RVR: "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (signal's own TPs used)",
        _AR:  "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (same ladder as Reversal Runner, "
              "capped-widened SL — signal's own TPs used)",
        _AR2: "5% TP1/2 · 10% TP3/4 · 15% TP5/6/7 · 25% TP8 (same ladder as Reversal Runner, "
              "fixed 10-pt SL — signal's own TPs used)",
    }),
    ("SL moves to BE", {
        _SO:  "After TP1",
        _BE:  "After TP1",
        _TS:  "Starts trailing at TP1",
        _PS:  "After TP2",
        _CO:  "Immediately at TP1 — SL moves to fill price (entry)",
        _NSS: "SL → TP1 at TP3 · steps TP{n-2} from TP4 onwards",
        _CT:  _ct_be_cell,
        _SR:  "At TP2 (+4 pts from fill) — SL moves to fill price (entry)",
        _SC:  _sc_be_cell,
        _RVR: "After TP1 → entry (BE); after TP2+ → trails to previous TP price",
        _AR:  "Immediately at TP1 → entry (BE); after TP2+ → trails to previous TP price "
              "(Reversal Runner waits until TP2 — Adaptive Runner doesn't need to, since its "
              "SL is already capped proportionate to the reachable reward)",
        _AR2: "At TP2 → entry (BE); after TP3+ → trails to the midpoint of the two TPs "
              "before the one just hit — not the single previous TP price every other "
              "ladder strategy uses",
    }),
    ("Max upside", {
        _SO:  "Capped at each TP",
        _BE:  "Highest TP",
        _TS:  "Unlimited (trend)",
        _PS:  "Capped from TP3",
        _CO:  "Unlimited (3-pt trailing stop after TP1)",
        _NSS: "Last TP of signal (max TP8)",
        _CT:  _ct_max_upside_cell,
        _SR:  "Unlimited (3-pt trailing stop after TP2 — full 50% runner)",
        _SC:  "Signal's final TP (TP6 typically 10-46 pts from entry on GD2 signals)",
        _RVR: "Signal's final TP (TP8 if present) — widened SL keeps the full ladder alive",
        _AR:  "Signal's final TP (TP8 if present) — SL widened only up to 50% of that "
              "distance, so the stop can never exceed the reward it's protecting",
        _AR2: "Signal's final TP (TP8 if present) — SL is a flat 10pts regardless of "
              "how far away that target actually is",
    }),
    ("Risk after TP1", {
        _SO:  "Zero (SL at entry from TP1)",
        _BE:  "Zero (SL at entry from TP1)",
        _TS:  "Trail distance only (trailing from TP1)",
        _PS:  "Full SL until TP2",
        _CO:  "Zero — trail floored at breakeven from TP1",
        _NSS: "1.5× emergency SL until TP3 (wide stop survives spikes)",
        _CT:  "Full SL until TP2",
        _SR:  "Full 10-pt SL until TP2, then zero — trail floored at breakeven",
        _SC:  "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _RVR: "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _AR:  "Zero after TP1 — SL locks to previous TP price after each subsequent level",
        _AR2: "Full 10-pt SL until TP2, then zero — SL locks to a two-TP-wide trailing "
              "midpoint after each subsequent level",
    }),
    ("Signal quality filter", {
        _SO:  "None",
        _BE:  _be_filter_cell,
        _TS:  "None",
        _PS:  "None",
        _CO:  "Direction only — signal SL/TP ignored entirely (5-pt SL / 3-pt TP1 from fill)",
        _NSS: "ADX > 30 required at entry — blocked in ranging/weak-trend conditions",
        _CT:  "Direction only — signal SL/TP ignored entirely",
        _SR:  "Direction only — signal SL/TP ignored entirely (10-pt SL / 3-pt TP1 / 4-pt TP2 from fill)",
        _SC:  "Full geometry validation — uses signal's SL and all TPs as-is",
        _RVR: "Full geometry validation — signal SL widened to min(4×, 20pt floor); TPs as-is",
        _AR:  "Full geometry validation — signal SL widened to min(4×, 20pt) then capped at "
              "50% of the final TP distance (never below the signal's own stated SL); TPs as-is",
        _AR2: "Direction + TP structure only — signal SL ignored entirely (fixed 10-pt SL "
              "from fill); TPs as-is",
    }),
    ("Best market", {
        _SO:  "Any",
        _BE:  _be_best_market_cell,
        _TS:  "Strong trend / breakout",
        _PS:  "Moderate trend / wider TPs",
        _CO:  "Any — tight scalp, quick TP1, small trail",
        _NSS: "Confirmed trend (ADX > 30) — GDV-style multi-TP signals",
        _CT:  "Any — fixed targets, low-maintenance",
        _SR:  "Any — tight scalp with two-stage confirmation before the 50% runner trails",
        _SC:  "Multi-TP professional signals (GD2, GDV) — built to ride the full TP ladder",
        _RVR: "Gold Diggers VIP zone-entry signals — built on 259-signal GDV backtest",
        _AR:  "Any multi-TP signal source of unknown/mixed ladder length — Gold Diggers VIP/"
              "GD2 and shorter-ladder channels alike; backtested 2026-07-15 against 226 real "
              "GDV/GD2 signals (+$400.29, PF 1.80, 5.8% max DD — lowest drawdown of every "
              "strategy tested there) and 309 Breakout/Bounce signals (still unprofitable "
              "there, like every strategy tested — that's an entry-quality issue, not "
              "something exit-strategy choice fixes)",
        _AR2: "Signals where a flat, predictable 10pt risk is preferred over the signal's "
              "own SL quality, and a two-level trail cushion is wanted instead of snapping "
              "to the immediately-prior TP — untested judgment call, not backtested",
    }),
]

# Table 1: core built-in strategies (left half)
_COMPARE_GROUP_1 = [_SO, _BE, _TS, _PS, _CO, _NSS]
# Table 2: advanced / specialised strategies + custom (right half)
_COMPARE_GROUP_2 = [_CT, _SR, _SC, _RVR, _AR, _AR2]


# ── Strategy ───────────────────────────────────────────────────────────────────

def _render_channel_strategy_card(engine, all_names: dict, rs: dict) -> None:
    """
    Compact channel strategy card: one row per channel with name, stats badge,
    and strategy dropdown all inline.  Rec label lives in a tooltip on the
    psychology icon to avoid stacking extra height.
    """
    import asyncio as _aio
    from forex_trader.core import database as _csdb
    from forex_trader.core import channel_strategy_ai as _csai
    from forex_trader.core import core_ea_templates as _et
    from forex_trader.core import core_trading_schedule as _csched
    from forex_trader.core.models import STRATEGY_NAMES

    # Schedule Override banner (2026-08-06). While the Trading Schedule is
    # enabled, the active window's own per-channel strategy/template pick
    # wins over everything selected on this card, for as long as that window
    # is active -- see core_trading_schedule.get_schedule_strategy_override
    # and the two sites that honour it (core_signal_resolution.py and
    # core_scan_messages_staleness_strategy.py). Without this the card reads
    # as authoritative when it may not be, which is exactly the confusion
    # that made a schedule-assigned template look like it was being ignored.
    # Shown purely on the schedule's enabled flag, not on whether a window
    # happens to be active right now: this renders once on page load and
    # would otherwise go stale the moment a window boundary passed.
    if _csched.is_trading_schedule_enabled():
        with ui.row().classes(
            "w-full items-center gap-2 mb-2 px-2 py-1 rounded "
            "bg-amber-900 border-l-4 border-amber-500"
        ):
            ui.icon("event_available", size="xs").classes("text-amber-300")
            ui.label("Schedule Override").classes(
                "text-xs font-bold text-amber-200"
            )
            ui.icon("info_outline", size="xs").classes(
                "text-amber-300 cursor-help"
            ).tooltip(
                "The Trading Schedule is on. Where the active window sets a "
                "strategy or template for a channel, that wins over the pick "
                "below for as long as the window is active. Windows with no "
                "override configured leave the selection below in effect."
            )

    with ui.row().classes("items-center gap-2 mb-1"):
        ui.label("Channel Strategy").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Assign a strategy per channel. Auto lets Claude evaluate market "
            "conditions and update the recommendation every 30 min."
        )

    channels = [
        ch for ch in _csdb.get_all_channel_strategy_settings()
        # ORB/IVB isn't a channel -- it's a time-of-day breakout engine with
        # its own page and its own orb_fixed management, so a per-channel
        # strategy row for it doesn't belong in this list. It stays a
        # canonical source everywhere else (trade attribution, scorecards,
        # rename cascades) and any override already stored for it is still
        # honoured by orb_auto_execute.
        if ch["source"] != "ORB/IVB Report"
    ]

    strat_opts = {"": "— Inherit Global —", "auto": "Auto (Claude)"}
    strat_opts.update(STRATEGY_NAMES)
    for k, v in all_names.items():
        if k not in strat_opts:
            strat_opts[k] = v
    # EA Templates -- a saved template fully replaces strategy dispatch for
    # a channel (the EA manages the trade end-to-end), so it's a peer entry
    # in the same list rather than a second selector.
    for _t in _et.list_ea_templates():
        strat_opts[_et.override_for_template(_t["name"])] = f"Template: {_t['name']}"

    _rec_icons: dict[str, object] = {}   # source → ui.icon for tooltip updates
    _sel_map:   dict[str, object] = {}   # source → ui.select

    with ui.column().classes("w-full gap-1"):
        for ch in channels:
            src     = ch["source"]
            is_auto = ch.get("auto_strategy", False)
            cur_ov  = "auto" if is_auto else (ch.get("strategy_override") or "")
            rec     = _csdb.get_channel_strategy_rec(src)
            pnl_col = "text-green-400" if (ch["net_pnl"] or 0) >= 0 else "text-red-400"
            rec_tip = _rec_label_text(rec, strat_opts)
            # Single stats label with fixed width keeps all dropdowns left-aligned
            stats_txt = f"WR {ch['win_rate']:.0f}% ${ch['net_pnl']:+.0f}"

            with ui.row().classes("items-center gap-1 w-full"):
                ui.label(src).classes(
                    "text-xs font-semibold text-gray-300 truncate shrink-0"
                ).style("width:7rem").tooltip(src)
                ui.label(stats_txt).classes(
                    f"text-xs font-mono {pnl_col} shrink-0"
                ).style("width:6rem")
                sel = ui.select(
                    strat_opts, value=cur_ov, label=None,
                ).classes("text-xs min-w-0").style("flex:1").props("dense outlined")
                rec_icon = ui.icon("psychology", size="xs").classes(
                    "text-purple-400 cursor-help shrink-0"
                ).tooltip(rec_tip or "No recommendation yet — click Evaluate")
                _rec_icons[src] = rec_icon
                _sel_map[src]   = sel

            def _on_change(e, _src=src, _icon=rec_icon):
                _v = e.value
                if isinstance(_v, dict):  # NiceGUI dict-options returns {label,value} obj
                    _v = _v.get("value", "")
                val = (_v or "") if _v is not None else ""
                is_a = (val == "auto")
                override = None if (val in ("", "auto")) else val
                _csdb.set_channel_strategy_override(_src, override, auto=is_a)
                status = "Auto (Claude)" if is_a else (
                    f"Manual: {strat_opts.get(val, val)}" if val else "Inheriting global"
                )
                ui.notify(f"{_src}: {status}", type="info", timeout=2500)

            sel.on_value_change(_on_change)

    # ── Evaluate Now + auto-refresh ──────────────────────────────────────────
    ui.separator().classes("my-1 border-gray-700")

    eval_status = ui.label("").classes("text-xs text-gray-500")

    def _update_rec_tooltips(results: dict) -> None:
        for src, _r in results.items():
            if src in _rec_icons:
                new_rec = _csdb.get_channel_strategy_rec(src)
                tip = _rec_label_text(new_rec, strat_opts)
                _rec_icons[src].tooltip(tip or "No recommendation yet")

    async def _refresh_tooltips_from_db() -> None:
        """
        Cheap per-client poll (DB reads only, no Claude call) so tooltips reflect
        the engine's own singleton background evaluation loop. The actual Claude
        evaluation runs once per engine in _channel_ai_auto_eval_loop — this must
        never call evaluate_channels() itself, or duplicate browser tabs/reconnects
        would again multiply real API calls.
        """
        try:
            def _fetch_recs():
                return {src: _csdb.get_channel_strategy_rec(src) for src in _rec_icons}

            # Offloaded — see _render_live_lines (settings.py) for why a
            # per-channel sync DB call directly in a timer callback matters.
            recs = await db_module.to_db_thread(_fetch_recs)
            for src, new_rec in recs.items():
                tip = _rec_label_text(new_rec, strat_opts)
                _rec_icons[src].tooltip(tip or "No recommendation yet")
            ts = __import__("datetime").datetime.now().strftime("%H:%M")
            eval_status.text = f"Updated {ts}"
        except Exception:
            pass

    def _apply_and_close(res: dict, dialog) -> None:
        for src, r in res.items():
            _csdb.set_channel_strategy_override(src, r["strategy"], auto=False)
            if src in _sel_map:
                _sel_map[src].value = r["strategy"]
        ui.notify("Recommendations applied to all channels", type="positive")
        dialog.close()

    async def _run_eval() -> None:
        """Called by the button — evaluates and shows results popup."""
        eval_status.text = "Evaluating…"
        cfg = engine._cfg if hasattr(engine, "_cfg") else {}
        try:
            results = await _csai.evaluate_channels(engine, cfg)
        except Exception as exc:
            eval_status.text = f"Failed: {exc}"
            ui.notify(f"Evaluation failed: {exc}", type="negative")
            return

        _update_rec_tooltips(results)
        ts = __import__("datetime").datetime.now().strftime("%H:%M")
        eval_status.text = f"Updated {ts}"
        used_ai = ai_provider.is_configured(cfg)
        _ai_label = "DeepSeek AI" if cfg.get("ai_provider") == "deepseek" else "Claude AI"

        # ── Results popup ────────────────────────────────────────────────────
        with ui.dialog().props("persistent") as dlg, \
             ui.card().classes("bg-gray-800 rounded-lg p-4 min-w-[480px] max-w-lg"):

            with ui.row().classes("items-center gap-2 mb-3"):
                ui.icon("psychology").classes("text-purple-400 text-xl")
                ui.label("Strategy Evaluation").classes(
                    "text-base font-bold text-yellow-300 flex-1"
                )
                ui.label(
                    f"{_ai_label if used_ai else 'Rule-based'} · {ts}"
                ).classes("text-xs text-gray-500")

            if not used_ai:
                ui.label(
                    "No AI provider configured — showing rule-based regime recommendations."
                ).classes("text-xs text-amber-400 italic mb-2")

            for src, r in results.items():
                strat_label = strat_opts.get(r["strategy"], r["strategy"])
                conf        = r.get("confidence", 0.0)
                reasoning   = r.get("reasoning", "")
                with ui.card().classes("bg-gray-900 rounded p-2 mb-1 w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(src).classes("text-xs font-semibold text-gray-200 flex-1")
                        ui.badge(strat_label, color="green").classes("text-xs")
                        ui.label(f"{conf:.0%}").classes("text-xs text-blue-300 font-mono")
                    if reasoning:
                        ui.label(reasoning).classes("text-xs text-gray-400 italic mt-0.5")

            ui.button(
                "Apply Recommendations", icon="check",
                on_click=lambda: _apply_and_close(results, dlg),
            ).classes("mt-3 bg-green-800 text-white text-xs w-full").props("dense")
            ui.button("Close", on_click=dlg.close).classes(
                "mt-1 text-xs w-full"
            ).props("flat dense")

        dlg.open()

    with ui.row().classes("items-center gap-2 mt-1"):
        ui.button(
            "Evaluate Now", icon="psychology",
            on_click=_run_eval,
        ).classes("text-xs bg-purple-800 text-white").props("dense").tooltip(
            "Ask Claude to evaluate current market conditions and recommend a strategy per channel. "
            "Results appear in a popup with an option to apply all at once."
        )
        eval_status

    ui.timer(60, _refresh_tooltips_from_db)


def _rec_label_text(rec: dict, strat_opts: dict) -> str:
    """Format the recommendation label under each channel dropdown."""
    strat = rec.get("strategy", "")
    reasoning = rec.get("reasoning", "")
    conf  = rec.get("confidence", 0.0)
    if not strat:
        return "No recommendation yet — click Evaluate Now"
    strat_name = strat_opts.get(strat, strat)
    conf_str   = f" ({conf:.0%})" if conf else ""
    reasoning_str = f" — {reasoning}" if reasoning else ""
    return f"Rec: {strat_name}{conf_str}{reasoning_str}"


def _render_strategy_params_card() -> None:
    """
    Strategy Parameters: live-editable SL/TP/close-% values for every
    fixed-parameter strategy (see core_strategy_params.PARAM_STRATEGIES),
    plus a small named-template library to save/reapply a parameter set
    later. A change here applies to the next trade opened under that
    strategy -- no restart, no code change. Mirrors a third-party EA's
    "Settings Templates" panel investigated 2026-07-22; see
    core_strategy_params.py's module docstring for why this needs no
    MQL5 changes at all (every strategy here is already fully resolved
    to concrete SL/TP prices by Python before any EA sees the trade).
    """
    from forex_trader.core import core_strategy_params as sp

    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("Strategy Parameters").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Live-editable SL/TP values for the fixed-parameter strategies -- "
            "a change applies to the next trade opened under that strategy, "
            "no restart needed. Save named presets below to switch between "
            "setups quickly."
        )

    state = {"strategy": sp.PARAM_STRATEGIES[0]}
    fields: dict[str, object] = {}

    def _on_strategy_change(e) -> None:
        v = e.value
        if isinstance(v, dict):  # NiceGUI dict-options returns {label,value} obj
            v = v.get("value")
        if v:
            state["strategy"] = v
            _draw_body()

    ui.select(
        sp.STRATEGY_LABELS, value=state["strategy"], label="Strategy",
    ).classes("w-56 mb-2").props("dense outlined").on_value_change(
        _on_strategy_change
    ).tooltip(
        "Which built-in strategy's fixed SL/TP point values to edit below. "
        "Changes apply to the next trade opened under that strategy."
    )

    body = ui.column().classes("w-full gap-2")

    def _current_values() -> dict:
        return {k: f.value for k, f in fields.items()}

    def _draw_body() -> None:
        body.clear()
        strategy = state["strategy"]
        specs = sp.PARAM_SPECS[strategy]
        live = sp.get_strategy_params(strategy)
        fields.clear()
        with body:
            with ui.row().classes("w-full gap-3 flex-wrap items-end"):
                for key, label, default, unit in specs:
                    step = 0.05 if unit in ("x", "frac") else 1.0
                    fields[key] = ui.number(
                        label=f"{label} ({unit})", value=live.get(key, default), step=step,
                        format="%.2f",
                    ).classes("w-36").props("dense outlined")

            with ui.row().classes("gap-2 mt-1"):
                ui.button("Save & Apply", on_click=_save_live).classes(
                    "text-xs bg-green-800 text-white"
                ).props("dense")
                ui.button("Reset to Default", on_click=_reset_default).classes(
                    "text-xs"
                ).props("dense outline")

            ui.separator().classes("my-2 border-gray-700")
            ui.label("Saved Templates").classes("text-sm font-semibold text-gray-300")

            templates = sp.list_templates(strategy)
            if not templates:
                ui.label("No saved templates for this strategy yet.").classes(
                    "text-xs text-gray-500"
                )
            else:
                for t in templates:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(t["name"]).classes(
                            "text-xs text-gray-200 truncate"
                        ).style("width:10rem").tooltip(t["name"])
                        ui.button("Apply", on_click=lambda _t=t: _apply_tpl(_t)).classes(
                            "text-xs"
                        ).props("dense flat color=blue")
                        ui.button(icon="delete_outline", on_click=lambda _t=t: _delete_tpl(_t)).props(
                            "dense flat color=red"
                        )

            with ui.row().classes("items-center gap-2 mt-2"):
                name_input = ui.input(placeholder="Template name").classes("w-48").props(
                    "dense outlined"
                )
                ui.button(
                    "Save as Template", on_click=lambda: _save_as_template(name_input)
                ).classes("text-xs").props("dense outline color=blue")

    def _save_live() -> None:
        strategy = state["strategy"]
        try:
            sp.set_strategy_params(strategy, _current_values())
            ui.notify(
                f"{sp.STRATEGY_LABELS[strategy]} parameters saved — applies to new trades",
                type="positive",
            )
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    def _reset_default() -> None:
        strategy = state["strategy"]
        sp.reset_strategy_params(strategy)
        ui.notify(f"{sp.STRATEGY_LABELS[strategy]} reset to defaults", type="info")
        _draw_body()

    def _apply_tpl(t: dict) -> None:
        try:
            sp.apply_template(t["id"])
            ui.notify(f"Applied template '{t['name']}'", type="positive")
            _draw_body()
        except Exception as exc:
            ui.notify(f"Apply failed: {exc}", type="negative")

    def _delete_tpl(t: dict) -> None:
        sp.delete_template(t["id"])
        ui.notify(f"Deleted template '{t['name']}'", type="info")
        _draw_body()

    def _save_as_template(name_input) -> None:
        strategy = state["strategy"]
        name = (name_input.value or "").strip()
        if not name:
            ui.notify("Enter a template name first", type="warning")
            return
        try:
            sp.save_template(strategy, name, _current_values())
            name_input.value = ""
            ui.notify(f"Saved template '{name}'", type="positive")
            _draw_body()
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    _draw_body()


def _render_global_parameters_card(rs: dict) -> None:
    """
    Global Parameters (2026-07-24): account-wide numbers that used to be
    scattered across the per-template EA Templates form, Active Strategy,
    and Risk Settings -- collected into one place since none of them are
    actually specific to a single template/strategy:

    - Harvest: moved from EA Templates' per-template harvest_enabled/
      harvest_threshold. Now applies to EVERY open position on the MT5
      account regardless of which strategy or template opened it (or
      whether it's EA-managed at all) -- pushed to the EA as a standing
      global config (ea_bridge.EABridge.push_global_config) rather than a
      per-trade field on open_trade, and the EA's OnTick sweeps every open
      position by ticket, not just the ones in its own g_trades[]/
      g_pending[] tracking. See ForexTraderBridge.mq5's
      CheckGlobalHarvest().
    - Fixed Lot Size (Single): moved from Active Strategy, same
      strategy_lot_size column/semantics (0 = risk-based auto) -- "fixed
      lot always wins" everywhere it's read (core_open_trade.py,
      core_fees_sizing.suggest_lot_size, core_manual_limit_order.py).
    - Fixed Lot Size (Grid): new. Used instead of the computed lot size
      for each leg of an EA Template in Grid mode -- see
      core_open_trade.py's template dispatch and
      ForexTraderBridge.mq5's HandleOpenTemplateGrid.
    - Risk per trade % / Max Risk per trade %: moved from Risk Settings,
      same risk_per_trade_pct/max_risk_per_trade_pct columns. Already fed
      into every strategy and template's lot sizing via
      core_fees_sizing.suggest_lot_size (risk_per_trade_pct as the base
      calculation, max_risk_per_trade_pct as an independent ceiling on
      top) -- moving them here is a pure UI relocation, no resolution
      logic changed.
    """
    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("Global Parameters").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Account-wide settings that apply to every strategy and template, "
            "regardless of which channel or EA Template opened the trade."
        )

    with ui.grid(columns=2).classes("w-full gap-3"):
        with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
            harvest_enabled = ui.switch(
                "Harvest", value=bool(rs.get("global_harvest_enabled", 0)),
            ).classes("text-sm")
            harvest_threshold = ui.number(
                "Profit threshold ($)", value=float(rs.get("global_harvest_threshold_usd", 50.0)),
                min=0.0, step=5.0,
            ).classes("w-full mt-1").props("dense outlined")
            ui.label(
                "Auto-close ANY open position (regardless of strategy, template, "
                "or how it was opened) once its own floating P&L reaches this "
                "amount."
            ).classes("text-xs text-gray-500 mt-1")

        with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
            fixed_lot_single = ui.number(
                "Fixed Lot Size (Single)", value=float(rs.get("strategy_lot_size", 0.0)),
                min=0.0, step=0.01, format="%.2f",
            ).classes("w-full").props("dense outlined")
            fixed_lot_single.tooltip(
                "Overrides risk-based sizing for every strategy and single-mode "
                "template. 0 = risk-based auto (Risk per trade %, below)."
            )
            fixed_lot_grid = ui.number(
                "Fixed Lot Size (Grid)", value=float(rs.get("strategy_lot_size_grid", 0.0)),
                min=0.0, step=0.01, format="%.2f",
            ).classes("w-full mt-2").props("dense outlined")
            fixed_lot_grid.tooltip(
                "Lot size used for EACH leg of an EA Template in Grid mode. "
                "0 = use the same lot as a normal (non-grid) trade."
            )

        with ui.card().classes("bg-gray-900 p-3 rounded-lg col-span-2"):
            with ui.row().classes("w-full gap-3"):
                risk_pct = ui.number(
                    "Risk per trade (%)", value=float(rs.get("risk_per_trade_pct", 0.5)),
                    min=0.01, max=100, step=0.1, format="%.2f",
                ).classes("flex-1").props("dense outlined")
                risk_pct.tooltip(
                    "Percentage of balance risked per trade — determines lot size "
                    "automatically when Fixed Lot Size is 0."
                )
                max_risk_pct = ui.number(
                    "Max Risk per trade (%)", value=float(rs.get("max_risk_per_trade_pct", 1.0)),
                    min=0.01, max=100, step=0.1, format="%.2f",
                ).classes("flex-1").props("dense outlined")
                max_risk_pct.tooltip(
                    "Hard ceiling — the risk-based lot size (above) is never allowed "
                    "to exceed this percentage of balance, regardless of Risk per "
                    "trade %. Does not apply when Fixed Lot Size is set."
                )

    def _save_global_params():
        try:
            db_module.update_risk_settings({
                "global_harvest_enabled":       int(bool(harvest_enabled.value)),
                "global_harvest_threshold_usd": float(harvest_threshold.value or 0),
                "strategy_lot_size":             float(fixed_lot_single.value or 0),
                "strategy_lot_size_grid":        float(fixed_lot_grid.value or 0),
                "risk_per_trade_pct":            float(risk_pct.value or 0),
                "max_risk_per_trade_pct":        float(max_risk_pct.value or 0),
            })
            from forex_trader.core import ea_bridge as _ea_mod
            _ea = _ea_mod.get_instance()
            if _ea is not None:
                asyncio.create_task(_ea.push_global_config())
            ui.notify("Global Parameters saved", type="positive")
        except Exception as ex:
            ui.notify(str(ex), type="negative")

    ui.button("Save Global Parameters", on_click=_save_global_params).classes(
        "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
    )


def _render_ea_templates_card() -> None:
    """
    EA Templates: complete, self-contained, EA-managed trade-management
    definitions (Grid vs Single, TP/SL visibility, trailing method,
    breakeven rule, cancel-pending-siblings) -- a channel can be assigned a
    saved template in the Channel Strategy card below in place of a
    built-in strategy. Unlike Strategy Parameters above (which only
    retunes existing Python-managed strategies), a template fully
    replaces strategy dispatch and the EA manages the trade end-to-end --
    every field here is sent fresh on each open, so changing a template's
    values never needs an EA recompile. See core_ea_templates.py's module
    docstring. Harvest moved to Global Parameters (below) 2026-07-24 --
    it now applies account-wide to every open position regardless of how
    it was opened, not just this template's own trades. Anchor TP (added
    2026-07-24): a per-TP pips/pct ladder -- pips fill any level the raw
    signal didn't supply, pct always wins over the signal (which never
    states a close percentage) -- see core_open_trade.py's EA-handoff block.
    """
    from forex_trader.core import core_ea_templates as et

    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("EA Templates").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Complete EA-managed trade definitions -- assign one to a channel "
            "(Channel Strategy, right) in place of a built-in strategy. The EA "
            "reads every field fresh on each open, no recompile needed."
        )

    state = {"name": None}  # None = new/unsaved template
    fields: dict[str, object] = {}

    body = ui.column().classes("w-full gap-2")

    def _load(name: Optional[str]) -> None:
        state["name"] = name
        _draw_body()

    def _current_values() -> dict:
        out = {}
        for k, f in fields.items():
            v = f.value
            if isinstance(v, dict):  # NiceGUI dict-options select
                v = v.get("value")
            out[k] = v
        return out

    def _copy_ladder(src_prefix: str, dst_prefix: str) -> None:
        """Copy one TP ladder onto the other (the panel's Copy to Pending /
        Copy to Anchor buttons). Anchor and pending ladders are separate by
        design -- the copier ships wider pending targets -- but starting one
        from the other and then tweaking is the common case."""
        for n in range(1, et.MAX_TP_LEVELS + 1):
            for suffix in ("pips", "pct"):
                src = fields.get(f"{src_prefix}{n}_{suffix}")
                dst = fields.get(f"{dst_prefix}{n}_{suffix}")
                if src is not None and dst is not None:
                    dst.value = src.value

    def _send_to_ea() -> None:
        """Push the form's current values to the live EA immediately.

        Templates are normally sent with each open_trade, so a saved change
        reaches the EA on the NEXT signal. This is the panel's green Send
        button: it pushes now, so an adjustment can be made mid-session
        without waiting for a new trade. No-ops harmlessly when the EA is
        not connected."""
        from forex_trader.core import ea_bridge as _eab
        ea = _eab.get_instance()
        if ea is None or not ea.is_ea_healthy():
            ui.notify("EA not connected — values saved, will apply on next signal",
                      type="warning")
            return
        name = (state["name"] or "").strip()
        if not name:
            ui.notify("Save the template first, then Send", type="warning")
            return
        try:
            from forex_trader.core.database import _schedule_coro
            _schedule_coro(ea.push_template(name, _current_values()))
            ui.notify(f"Sent '{name}' to the EA", type="positive")
        except Exception as exc:
            ui.notify(f"Send failed: {exc}", type="negative")

    def _export_templates() -> None:
        """Save every saved template to a shareable file.

        Exports what is in the Load drop-down (i.e. the saved templates),
        NOT the unsaved values currently in the form -- an edit has to be
        saved before it can be exported, same as it has to be saved before
        a channel can use it."""
        try:
            saved = et.list_ea_templates()
            if not saved:
                ui.notify("No saved templates to export", type="warning")
                return
            ui.download.content(
                et.export_templates(), et.export_filename(),
                media_type="application/json",
            )
            ui.notify(f"Exporting {len(saved)} template(s)", type="positive")
        except Exception as exc:
            ui.notify(f"Export failed: {exc}", type="negative")

    def _build_import_dialog():
        """Build the Import popup once, as a sibling of `body`.

        Deliberately NOT built inside `body`: the import handler ends with
        a _draw_body(), which clears `body` -- a dialog living in there
        would be destroyed out from under its own running handler."""
        with ui.dialog() as dlg, ui.card().classes(
            "bg-gray-900 border border-gray-700 p-4 gap-2 min-w-96"
        ):
            ui.label("Import Templates").classes(
                "text-sm font-bold text-yellow-300")
            ui.label(
                f"Choose a template file ({et.EXPORT_EXTENSION} or .json) exported "
                "from another install. Its templates are added to your Load list."
            ).classes("text-xs text-gray-400")
            overwrite = ui.checkbox("Overwrite templates with the same name") \
                .classes("text-xs").tooltip(
                    "Off: a template whose name you already use is skipped and "
                    "your own version is kept. On: the file's version replaces yours."
                )

            def _handle(e) -> None:
                try:
                    res = et.import_templates(
                        e.content.read(), overwrite=bool(overwrite.value))
                except Exception as exc:
                    ui.notify(f"Import failed: {exc}", type="negative")
                    return
                dlg.close()
                parts = []
                for label, key in (("added", "added"), ("replaced", "replaced"),
                                   ("skipped", "skipped")):
                    if res[key]:
                        parts.append(f"{len(res[key])} {label}")
                if not parts:
                    ui.notify("File contained no templates", type="warning")
                    return
                ui.notify(
                    f"Imported from {e.name}: " + ", ".join(parts),
                    type="positive" if (res["added"] or res["replaced"]) else "warning",
                )
                if res["skipped"]:
                    ui.notify(
                        "Kept your existing: " + ", ".join(res["skipped"])
                        + " — re-import with Overwrite ticked to replace them.",
                        type="info",
                    )
                _draw_body()

            uploader = ui.upload(
                on_upload=_handle, auto_upload=True, max_files=1,
                max_file_size=8 * 1024 * 1024, label="Select template file",
            ).classes("w-full").props('accept=".json,.eatpl" flat dense')
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dlg.close) \
                    .classes("text-xs").props("dense flat")
        return dlg, uploader

    _import_dialog, _import_uploader = _build_import_dialog()

    def _open_import_dialog() -> None:
        # Clear any previous run's file chip so a second import starts
        # from an empty picker rather than the last file's name.
        _import_uploader.reset()
        _import_dialog.open()

    def _draw_body() -> None:
        body.clear()
        live = (et.get_ea_template(state["name"]) if state["name"] else None) or dict(et.DEFAULTS)
        fields.clear()
        N = et.MAX_TP_LEVELS
        with body:
            with ui.row().classes("items-center gap-2 mb-2"):
                existing = et.list_ea_templates()
                load_opts = {"": "— New Template —"}
                load_opts.update({t["name"]: t["name"] for t in existing})
                ui.select(
                    load_opts, value=state["name"] or "", label="Load",
                ).classes("w-56").props("dense outlined").on_value_change(
                    lambda e: _load(e.value or None)
                ).tooltip(
                    "Load a saved template's values into the form for editing, "
                    "or leave on \"New Template\" to build one from scratch."
                )
                name_input = ui.input(
                    "Template name", value=state["name"] or "",
                ).classes("w-56").props("dense outlined")
                # Import/Export (2026-08-06) -- move templates between
                # installs/users. Both open the browser's own file
                # dialog: Import via the upload picker in the dialog
                # below, Export via a download (Save As).
                ui.button(
                    "Import Templates", icon="upload_file",
                    on_click=_open_import_dialog,
                ).classes("text-xs bg-blue-800 text-white px-3") \
                    .props("dense unelevated").tooltip(
                        "Load templates from a template file shared by another "
                        "user. Existing templates of the same name are kept "
                        "unless you tick Overwrite."
                    )
                ui.button(
                    "Export Templates", icon="download",
                    on_click=_export_templates,
                ).classes("text-xs bg-blue-900 text-white px-3") \
                    .props("dense unelevated").tooltip(
                        "Save every template in the Load list to a "
                        f"{et.EXPORT_EXTENSION} file you can share or keep as a backup."
                    )

            # ── Section header helper ────────────────────────────────────
            # Every major block below is its own bordered card with a
            # colour-coded header -- previously these floated directly on
            # the page background with identical flat-gray labels, which
            # is what made the whole editor read as one undifferentiated
            # wall of fields. Anchor TP and Pending TP get their own
            # accent colours specifically so the two ladders are
            # distinguishable at a glance without reading every label.
            def _section(title: str, color: str, tip: str = ""):
                card = ui.card().classes(
                    "w-full bg-gray-900 border border-gray-700 rounded-lg p-3 mb-2"
                )
                with card:
                    with ui.row().classes("items-center gap-1 mb-1"):
                        ui.label(title).classes(
                            f"text-xs font-bold uppercase tracking-wider {color}"
                        )
                        if tip:
                            ui.icon("info_outline", size="14px").classes(
                                "text-blue-400 cursor-help").tooltip(tip)
                return card

            # ── Entries & lots ────────────────────────────────────────────
            with _section("Entries & Lots", "text-gray-200"):
                with ui.row().classes("w-full gap-2 mb-1"):
                    def _num(key, label, step, tip, width="w-28", mn=0):
                        with ui.column().classes("gap-0"):
                            with ui.row().classes("items-center gap-1"):
                                ui.label(label).classes("text-xs text-gray-400")
                                if tip:
                                    ui.icon("info_outline", size="14px").classes(
                                        "text-blue-400 cursor-help").tooltip(tip)
                            fields[key] = ui.number(
                                value=live[key], step=step, min=mn,
                            ).classes(width).props("dense outlined")

                    _num("anchors", "Anchors", 1,
                         "How many legs enter immediately at market when the signal "
                         "arrives. The anchor takes part of the position straight "
                         "away so a signal that never retraces isn't missed entirely. "
                         "0 = pending legs only.")
                    _num("pendings", "Pendings", 1,
                         "How many resting limit legs are staged inside the signal's "
                         "entry zone, waiting for a better fill than the anchor got.")
                    _num("lot_anchor", "Anchor Lot", 0.01,
                         "Lot size for each anchor (market) leg.")
                    _num("lot_pending", "Pending Lot", 0.01,
                         "Lot size for each pending (limit) leg.")
                    _num("sl_pips", "SL (pips)", 1.0,
                         "Stop distance in pips, used when the signal doesn't supply "
                         "its own SL. 10 pips = 1.00 of gold price, so 50 = $5.00 per "
                         "0.01 lot. The signal's own SL always wins when present.")
                    _num("grid_step_pts", "Ladder Step", 1.0,
                         "Spacing between pending legs, in pips. Always used in "
                         "STEP pending mode; in ZONE mode it only applies when the "
                         "signal states no entry zone of its own.")
                    _num("risk_pct", "Risk % (0=OFF)", 0.1,
                         "Size legs from account risk instead of the fixed lots "
                         "above. 0 = use the fixed lots.")

            # ── TP ladders ────────────────────────────────────────────────
            def _tg_tp_switch(key: str, which: str):
                """"Use TP Levels from Telegram" for one ladder.

                On, the pips row below is ignored and the levels come from the
                triggering Telegram message's own TP prices. The pips values
                are kept (and stay visible, just disabled) because they are
                still what the internal signal generators use -- those have no
                message to read, so this switch never applies to them."""
                with ui.row().classes("items-center gap-2 mb-2"):
                    sw = ui.switch(
                        "Use TP Levels from Telegram",
                        value=bool(live[key]),
                    ).classes("text-xs")
                    fields[key] = sw
                    ui.icon("info_outline", size="14px").classes(
                        "text-blue-400 cursor-help").tooltip(
                        f"ON: the {which} legs take their TP levels from the "
                        f"Telegram message's own TP prices, and the pips row "
                        f"below is ignored. The message sets how many levels "
                        f"there are; the % row still decides how much closes "
                        f"at each, and (when Close Full On Last TP above is "
                        f"on) the last level closes the remainder.\n\n"
                        f"Internal signals (Reversal, Breakout, Bounce, ORB) "
                        f"have no message, so they keep using the pips row "
                        f"regardless. A Telegram signal that states no TPs "
                        f"also falls back to it.")
                return sw

            def _ladder_grid(prefix: str, tg_switch=None) -> None:
                with ui.grid(columns=N + 1).classes("w-full gap-1"):
                    ui.label("").classes("text-xs")
                    for n in range(1, N + 1):
                        ui.label(f"TP{n}").classes("text-xs text-center text-gray-400")
                    ui.label("pips from entry").classes("text-xs text-gray-500 self-center")
                    for n in range(1, N + 1):
                        num = ui.number(
                            value=float(live[f"{prefix}{n}_pips"]), step=1.0, min=0,
                        ).classes("w-full").props("dense outlined")
                        if tg_switch is not None:
                            # Disabled, not hidden or cleared: these values
                            # still drive internal-generator trades, so they
                            # must keep their values and keep saving.
                            num.bind_enabled_from(
                                tg_switch, "value", backward=lambda v: not v)
                        fields[f"{prefix}{n}_pips"] = num
                    ui.label("% of trade to close").classes("text-xs text-gray-500 self-center")
                    for n in range(1, N + 1):
                        fields[f"{prefix}{n}_pct"] = ui.number(
                            value=float(live[f"{prefix}{n}_pct"]), step=1.0, min=0, max=100,
                        ).classes("w-full").props("dense outlined")

            with _section(
                "Anchor TP", "text-amber-400",
                "Targets for the anchor (market) legs, in pips from entry. "
                "These are AUTHORITATIVE -- they replace whatever TP levels "
                "the triggering signal itself stated, so this channel behaves "
                "identically regardless of message shape. A level left at 0 "
                "is simply not used. The % row is always template-driven, "
                "since a signal never states how much to close at each level.",
            ):
                _ladder_grid("tp", _tg_tp_switch("tp_from_telegram", "anchor"))
                with ui.row().classes("gap-2 mt-2"):
                    ui.button("Copy to Pending ↓",
                              on_click=lambda: _copy_ladder("tp", "tp_pen")) \
                        .classes("text-xs bg-sky-700 text-white").props("dense unelevated")
                    ui.button("↑ Copy to Anchor",
                              on_click=lambda: _copy_ladder("tp_pen", "tp")) \
                        .classes("text-xs bg-amber-600 text-white").props("dense unelevated")

            with _section(
                "Pending TP", "text-sky-400",
                "Separate targets for the resting (limit) legs. Usually set "
                "WIDER than the anchor ladder: a leg filled deeper in the "
                "zone has more room to the same structural level. With "
                "Anchor = Unified every leg shares one target PRICE, so a "
                "deeper leg automatically earns more points reaching it. "
                "Leave at 0 to reuse the anchor ladder.",
            ):
                _ladder_grid("tp_pen",
                             _tg_tp_switch("tp_pen_from_telegram", "pending"))

            # ── Strategy toggles ──────────────────────────────────────────
            strategy_section = _section("Strategy", "text-emerald-400")
            with strategy_section, ui.grid(columns=3).classes("w-full gap-2 mb-1"):
                def _toggle(key, label, opts, tip):
                    with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                        with ui.row().classes("items-center gap-1"):
                            ui.label(label).classes("text-xs text-gray-300")
                            ui.icon("info_outline", size="14px").classes(
                                "text-blue-400 cursor-help").tooltip(tip)
                        fields[key] = ui.toggle(
                            opts, value=live[key],
                        ).props("dense no-caps").classes("text-xs")

                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("TG CMD").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Let Logic Keywords (CLOSE ALL / RISK FREE / TP HIT) "
                            "from the channel act on trades opened under this "
                            "template.")
                    fields["tg_cmd_enabled"] = ui.switch(
                        "", value=bool(live["tg_cmd_enabled"])).classes("text-xs")
                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Harvest").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Close the whole basket once its combined floating "
                            "profit clears the harvest threshold, regardless of "
                            "individual TP levels.")
                    fields["harvest_enabled"] = ui.switch(
                        "", value=bool(live["harvest_enabled"])).classes("text-xs")
                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Close Full On Last TP").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "ON (default): the last CONFIGURED Anchor TP level "
                            "closes whatever remains outright, regardless of "
                            "its own %. OFF: that level closes only its own %, "
                            "leaving the remainder open to run under Trail/BE "
                            "below -- for a ladder whose %s add up to well "
                            "under 100 and is meant to leave a genuine runner "
                            "instead of being flattened at the last level.")
                    fields["close_full_on_last"] = ui.switch(
                        "", value=bool(live["close_full_on_last"])).classes("text-xs")
                _toggle("mode", "Mode", {"grid": "GRID", "single": "SINGLE"},
                        "GRID stages anchor + pending legs across the signal's "
                        "zone. SINGLE opens one position.")
                _toggle("pending_mode", "Pending",
                        {"zone": "ZONE", "step": "STEP"},
                        "Where GRID rests its pending legs. ZONE spreads them "
                        "across the signal's own stated entry zone, honouring the "
                        "levels the signal named -- but a leg lands on the wrong "
                        "side of the market and is skipped if price has already "
                        "left that zone. STEP places them Ladder Step pips from "
                        "the anchor instead, which is what the reference copier "
                        "does and can never be skipped for being wrong-side.")
                _toggle("tpsl_mode", "TP/SL",
                        {"off": "OFF", "on": "ON", "stealth": "STEALTH"},
                        "ON puts real SL/TP on the broker order. STEALTH keeps "
                        "targets internal to the EA so they're not visible to "
                        "the broker. OFF sets neither.")
                _toggle("anchor", "Anchor",
                        {"unified": "UNIFIED", "distributed": "DISTRIBUTED"},
                        "UNIFIED: every leg shares one breakeven and one target "
                        "PRICE measured from the group's base, so a deeper leg "
                        "earns more points. DISTRIBUTED: each leg uses its own "
                        "fill price, giving every leg equal distance.")
                _toggle("trail_mode", "Trail",
                        {"off": "OFF", "candle": "CANDLE", "step": "STEP",
                         "fractal": "FRACTAL", "tp": "TP"},
                        "How the stop follows price. TP trails to the last "
                        "cleared TP level; CANDLE/FRACTAL follow structure; "
                        "STEP uses the fixed trail distance below.")

            with strategy_section, ui.row().classes("w-full gap-2 mb-1"):
                _num("trail_distance", "Trail Dist", 1.0,
                     "Stop distance behind price for STEP trailing, in pips.")
                _num("trail_activation", "Trail Activate", 1.0,
                     "Hold the stop still until the trade is this many pips in "
                     "profit. 0 = trail from the start. Independent of Trail "
                     "Trigger below (Triggers section) -- trailing arms as soon "
                     "as EITHER condition is met, whichever comes first.")
                _num("trail_step", "Trail Step", 1.0,
                     "Minimum move before the stop is adjusted again.")
                _num("harvest_threshold", "Harvest $", 1.0,
                     "Basket floating profit (account currency) that triggers a "
                     "harvest close.")

            # ── Triggers ──────────────────────────────────────────────────
            triggers_section = _section("Triggers", "text-violet-400")
            with triggers_section, ui.row().classes("w-full gap-2 mb-1"):
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("BE Mode").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Where breakeven puts the stop: exactly at entry, or "
                            "entry plus a small buffer to cover costs.")
                    fields["be_mode"] = ui.select(
                        {"entry": "ENTRY", "entry_buffer": "ENTRY + BUFFER"},
                        value=live["be_mode"],
                    ).classes("w-44").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("BE Trigger").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level moves the stop to breakeven. Worth "
                            "knowing: measured on this account's own trade "
                            "paths, breakeven moves REDUCED expectancy in every "
                            "configuration tested — see tools/exit_policy_lab.py.")
                    fields["be_trigger"] = ui.select(
                        {n: f"TP{n}" for n in range(1, N + 1)},
                        value=int(live["be_trigger"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trail Trigger").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level arms trailing, instead of (or "
                            "alongside) Trail Activate's raw pip distance above "
                            "-- whichever condition is met first. OFF leaves "
                            "Trail Activate as the only arm condition. Confirmed "
                            "live on Asian - Grid: Trail Activate's default (100 "
                            "pips) sat deeper than the template's own last "
                            "defined TP (50 pips), so the runner never armed at "
                            "all and every winning trade capped at the same "
                            "~$43 regardless of how far price actually ran.")
                    fields["tp1_trigger_level"] = ui.select(
                        {0: "OFF", **{n: f"TP{n}" for n in range(1, N + 1)}},
                        value=int(live["tp1_trigger_level"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Cancel Pending").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level cancels any still-resting sibling "
                            "legs. OFF leaves them on the book to fill later.")
                    fields["cancel_pending_level"] = ui.select(
                        {0: "OFF", **{n: f"TP{n}" for n in range(1, N + 1)}},
                        value=int(live["cancel_pending_level"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Sig Guard").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Block a new trade on this channel while one is "
                            "already open in the same direction. Use the pips "
                            "box beside this to only block when the open trade "
                            "is that close to the new one.")
                    fields["sig_guard"] = ui.switch(
                        "", value=bool(live["sig_guard"])).classes("text-xs")
                _num("sig_guard_pips", "Sig Guard pips", 1.0,
                     "0 = block on ANY open same-direction trade for this channel. "
                     "Above 0, only an open trade whose entry is within this many "
                     "pips blocks, so a genuinely separate setup further down the "
                     "chart can still trade. The reference copier shows this as "
                     "\"SIG GUARD: 20p\".")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Group TP Action").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Grid only: the first TP any leg clears cancels the "
                            "resting siblings and moves the live ones to their "
                            "own breakeven.")
                    fields["group_tp_action"] = ui.switch(
                        "", value=bool(live["group_tp_action"])).classes("text-xs")

            # ── Guards & execution ────────────────────────────────────────
            with _section("Guards & Execution", "text-rose-400"), \
                 ui.row().classes("w-full gap-2 mb-1"):
                _num("equity_protect", "Equity Protect $", 1.0,
                     "Close everything on this template if floating loss exceeds "
                     "this many account-currency units. 0 = off.")
                _num("guard_pips", "Guard pips", 1.0,
                     "Minimum distance to keep between a stop and current price. "
                     "This is what prevents a breakeven move being rejected as "
                     "an invalid stop when price has already run past entry.")
                _num("max_spread_pips", "Max Spread", 0.5,
                     "Skip the trade if the spread is wider than this at fill "
                     "time.")
                _num("late_guard_pips", "Late Guard", 1.0,
                     "Reject a signal that arrives this many pips beyond its own "
                     "zone. 0 = no guard.")
                _num("signal_max_age_sec", "Max Age (s)", 1,
                     "Ignore a signal older than this many seconds at fill time.")

            with ui.row().classes("gap-2 mt-3"):
                ui.button(
                    "Save Template", on_click=lambda: _save(name_input),
                ).classes("text-xs font-semibold bg-green-700 text-white px-4") \
                    .props("dense unelevated")
                ui.button("Send to EA", on_click=lambda: _send_to_ea()) \
                    .classes("text-xs bg-green-800 text-white").props("dense") \
                    .tooltip(
                        "Push these values to the running EA right now. Without "
                        "this they still apply, but only from the next signal "
                        "onward (a template is sent with every trade open)."
                    )
                if state["name"]:
                    ui.button(
                        "Delete", on_click=lambda: _delete(state["name"]),
                    ).classes("text-xs font-semibold bg-red-800 text-white px-4") \
                        .props("dense unelevated")
                ui.button(
                    "New", on_click=lambda: _load(None),
                ).classes("text-xs px-4").props("dense outline")

    def _save(name_input) -> None:
        name = (name_input.value or "").strip()
        if not name:
            ui.notify("Enter a template name first", type="warning")
            return
        try:
            et.save_ea_template(name, _current_values())
            ui.notify(f"Saved template '{name}'", type="positive")
            state["name"] = name
            _draw_body()
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    def _delete(name: str) -> None:
        et.delete_ea_template(name)
        ui.notify(f"Deleted template '{name}'", type="info")
        _load(None)

    _draw_body()


def render_signals_card() -> None:
    """Per-source live-execution toggles (Telegram/Bounce/Breakout/Reversal Engine) --
    self-contained, importable by other pages (moved to the top of the
    Parsing page, 2026-07-22 -- was previously part of Trading > Strategy)."""
    rs = db_module.get_risk_settings()
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
        ui.label("Signals").classes("text-base font-bold text-yellow-300 mb-3")

        with ui.row().classes("w-full gap-6 flex-wrap items-start"):

            # ── Telegram Signals ──────────────────────────────────────────
            with ui.column().classes("gap-1 min-w-52"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Telegram Signals").classes("text-sm font-semibold text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When ON, Telegram signals from your configured channels are parsed "
                        "and can be auto-executed. When OFF, incoming Telegram messages are "
                        "collected but completely ignored — no signals are created or traded."
                    )
                ui.label("Accept and trade signals from Telegram channels").classes(
                    "text-xs text-gray-500 mb-1"
                )
                _tg_on = bool(rs.get("accept_tg_signals", 1))
                tg_sig_badge = ui.badge(
                    "TG SIGNALS ON" if _tg_on else "TG SIGNALS OFF",
                    color="green" if _tg_on else "grey",
                )

                def toggle_tg_signals(badge=tg_sig_badge):
                    cur = bool(db_module.get_risk_settings().get("accept_tg_signals", 1))
                    db_module.update_risk_settings({"accept_tg_signals": 0 if cur else 1})
                    new = not cur
                    badge.props(f"color={'green' if new else 'grey'}")
                    badge.text = "TG SIGNALS ON" if new else "TG SIGNALS OFF"
                    ui.notify(
                        "Telegram signals enabled" if new else "Telegram signals disabled — incoming messages will be ignored",
                        type="positive" if new else "warning",
                    )

                ui.button("Toggle", icon="swap_horiz", on_click=toggle_tg_signals).classes(
                    "text-xs mt-1"
                )

            ui.separator().props("vertical").classes("hidden sm:block")

            # ── Bounce Generator ──────────────────────────────────────────
            with ui.column().classes("gap-1 min-w-52"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Bounce Generator").classes("text-sm font-semibold text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When ON, signals generated by the Bounce Generator are "
                        "executed as real MT5 trades using the active strategy and risk settings. "
                        "When OFF, generated signals remain virtual only. "
                        "Use Bounce Generator tab to start/stop the engine itself."
                    )
                ui.label("Route bounce signals to MT5").classes(
                    "text-xs text-gray-500 mb-1"
                )
                _sg_on = bool(rs.get("sg_live_execution", 0))
                sg_live_badge = ui.badge(
                    "SG LIVE ON" if _sg_on else "SG LIVE OFF",
                    color="green" if _sg_on else "grey",
                )

                def toggle_sg_live(badge=sg_live_badge):
                    cur = bool(db_module.get_risk_settings().get("sg_live_execution", 0))
                    new = not cur
                    db_module.update_risk_settings({"sg_live_execution": 1 if new else 0})
                    badge.props(f"color={'green' if new else 'grey'}")
                    badge.text = "SG LIVE ON" if new else "SG LIVE OFF"
                    ui.notify(
                        "Bounce Generator live execution ON — signals will place real MT5 trades"
                        if new else "Bounce Generator live execution OFF",
                        type="positive" if new else "info",
                    )

                ui.button("Toggle", icon="swap_horiz", on_click=toggle_sg_live).classes(
                    "text-xs mt-1"
                )

                ui.separator().classes("my-1")
                ui.label("AI signal evaluation").classes("text-xs text-gray-500 mb-1")
                _sg_claude_on = bool(rs.get("sg_claude_eval_enabled", 1))
                sg_claude_badge = ui.badge(
                    "AI ON" if _sg_claude_on else "AI OFF",
                    color="blue" if _sg_claude_on else "grey",
                )

                async def toggle_sg_claude(badge=sg_claude_badge):
                    key = "sg_claude_eval_enabled"
                    if _is_remote_active():
                        cli = sync_client.get_instance()
                        if cli is None:
                            ui.notify("Not connected to VPS", type="negative")
                            return
                        # Read "current" from the VPS's own confirmed settings
                        # snapshot, not this node's local DB row — the RPC below
                        # never updates that local row, only the periodic full
                        # settings broadcast does, so basing "current" on the
                        # local row made every click here recompute the same
                        # "new" value forever (toggle stuck one-directional:
                        # confirmed live — Bounce stuck OFF, Breakout stuck ON,
                        # each click just re-sent the same target state).
                        cur = bool(cli.remote_settings.get(key, 1))
                        new = not cur
                        try:
                            ack = await cli.send_engine_control("bounce", "set_ai_eval", enabled=new)
                        except Exception as exc:
                            ui.notify(f"Failed to reach VPS: {exc}", type="negative")
                            return
                        if ack.get("error"):
                            ui.notify(f"VPS rejected: {ack['error']}", type="negative")
                            return
                        cli.remote_settings[key] = 1 if new else 0
                    else:
                        cur = bool(db_module.get_risk_settings().get(key, 1))
                        new = not cur
                        db_module.update_risk_settings({key: 1 if new else 0})
                    badge.props(f"color={'blue' if new else 'grey'}")
                    badge.text = "AI ON" if new else "AI OFF"
                    ui.notify(
                        "Bounce: AI evaluation ON — each signal reviewed by AI"
                        if new else
                        "Bounce: AI evaluation OFF — ML + rules only (faster)",
                        type="info",
                    )

                ui.button("Toggle", icon="psychology", on_click=toggle_sg_claude).classes(
                    "text-xs mt-1"
                )

            ui.separator().props("vertical").classes("hidden sm:block")

            # ── Breakout Generator ────────────────────────────────────────
            with ui.column().classes("gap-1 min-w-52"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Breakout Generator").classes("text-sm font-semibold text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When ON, confirmed breakout signals are sent to MT5 as live trades. "
                        "When OFF, the engine runs in virtual/learning-only mode. "
                        "Use Breakout Generator tab to start/stop the engine and view signals."
                    )
                ui.label("Route breakout signals to MT5 as live trades").classes(
                    "text-xs text-gray-500 mb-1"
                )
                _bo_live_on = bool(rs.get("bo_live_execution", 0))
                bo_live_badge = ui.badge(
                    "BO LIVE ON" if _bo_live_on else "BO LIVE OFF",
                    color="green" if _bo_live_on else "grey",
                )

                def toggle_bo_live(badge=bo_live_badge):
                    cur = bool(db_module.get_risk_settings().get("bo_live_execution", 0))
                    new = not cur
                    db_module.update_risk_settings({"bo_live_execution": 1 if new else 0})
                    badge.props(f"color={'green' if new else 'grey'}")
                    badge.text = "BO LIVE ON" if new else "BO LIVE OFF"
                    ui.notify(
                        "Breakout live execution ENABLED — signals will open real MT5 trades" if new
                        else "Breakout live execution DISABLED — virtual mode only",
                        type="positive" if new else "warning",
                    )

                # Sync badge to live state on each page refresh (30s timer)
                def _sync_bo_badge(badge=bo_live_badge):
                    _live = bool(db_module.get_risk_settings().get("bo_live_execution", 0))
                    badge.text = "BO LIVE ON" if _live else "BO LIVE OFF"
                    badge.props(f"color={'green' if _live else 'grey'}")
                ui.timer(30, _sync_bo_badge)

                ui.button("Toggle", icon="swap_horiz", on_click=toggle_bo_live).classes(
                    "text-xs mt-1"
                )

                ui.separator().classes("my-1")
                ui.label("AI signal evaluation").classes("text-xs text-gray-500 mb-1")
                _bo_claude_on = bool(rs.get("bo_claude_eval_enabled", 1))
                bo_claude_badge = ui.badge(
                    "AI ON" if _bo_claude_on else "AI OFF",
                    color="blue" if _bo_claude_on else "grey",
                )

                async def toggle_bo_claude(badge=bo_claude_badge):
                    key = "bo_claude_eval_enabled"
                    if _is_remote_active():
                        cli = sync_client.get_instance()
                        if cli is None:
                            ui.notify("Not connected to VPS", type="negative")
                            return
                        # See toggle_sg_claude's comment above — "current" must
                        # come from the VPS's confirmed settings snapshot, not
                        # this node's local DB row, or the toggle sticks in one
                        # direction forever.
                        cur = bool(cli.remote_settings.get(key, 1))
                        new = not cur
                        try:
                            ack = await cli.send_engine_control("breakout", "set_ai_eval", enabled=new)
                        except Exception as exc:
                            ui.notify(f"Failed to reach VPS: {exc}", type="negative")
                            return
                        if ack.get("error"):
                            ui.notify(f"VPS rejected: {ack['error']}", type="negative")
                            return
                        cli.remote_settings[key] = 1 if new else 0
                    else:
                        cur = bool(db_module.get_risk_settings().get(key, 1))
                        new = not cur
                        db_module.update_risk_settings({key: 1 if new else 0})
                    badge.props(f"color={'blue' if new else 'grey'}")
                    badge.text = "AI ON" if new else "AI OFF"
                    ui.notify(
                        "Breakout: AI evaluation ON — each signal reviewed by AI"
                        if new else
                        "Breakout: AI evaluation OFF — ML + rules only (faster execution)",
                        type="info",
                    )

                ui.button("Toggle", icon="psychology", on_click=toggle_bo_claude).classes(
                    "text-xs mt-1"
                )

            ui.separator().props("vertical").classes("hidden sm:block")

            # ── Reversal Engine ────────────────────────────────────────────
            with ui.column().classes("gap-1 min-w-52"):
                with ui.row().classes("items-center gap-1"):
                    ui.label("Reversal Engine").classes("text-sm font-semibold text-gray-200")
                    ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                        "When ON, signals generated by the Reversal Engine are executed as "
                        "real MT5 trades. The engine reverse-engineers Gold Diggers VIP's "
                        "methodology to generate signals before they arrive on Telegram. "
                        "Use Signal Generator > Reversal Engine tab to view signals and correlation stats."
                    )
                ui.label("Route Reversal Engine signals to MT5 as live trades").classes(
                    "text-xs text-gray-500 mb-1"
                )
                _re_live_on = bool(rs.get("re_live_execution", 0))
                re_live_badge = ui.badge(
                    "RE LIVE ON" if _re_live_on else "RE LIVE OFF",
                    color="green" if _re_live_on else "grey",
                )

                def toggle_re_live(badge=re_live_badge):
                    cur = bool(db_module.get_risk_settings().get("re_live_execution", 0))
                    new = not cur
                    db_module.update_risk_settings({"re_live_execution": 1 if new else 0})
                    badge.props(f"color={'green' if new else 'grey'}")
                    badge.text = "RE LIVE ON" if new else "RE LIVE OFF"
                    ui.notify(
                        "Reversal Engine live execution ENABLED — signals will open real MT5 trades" if new
                        else "Reversal Engine live execution DISABLED — virtual mode only",
                        type="positive" if new else "warning",
                    )

                def _sync_re_badge(badge=re_live_badge):
                    _live = bool(db_module.get_risk_settings().get("re_live_execution", 0))
                    badge.text = "RE LIVE ON" if _live else "RE LIVE OFF"
                    badge.props(f"color={'green' if _live else 'grey'}")
                ui.timer(30, _sync_re_badge)

                ui.button("Toggle", icon="swap_horiz", on_click=toggle_re_live).classes(
                    "text-xs mt-1"
                )


def _render_strategy(engine):
    outer = ui.column().classes("w-full gap-4")

    def _refresh():
        outer.clear()
        with outer:
            _draw()

    def _draw():  # noqa: C901  (complex but linear)
        rs            = db_module.get_risk_settings()
        custom_strats = db_module.get_custom_strategies()
        _hidden       = _get_hidden_strategies()
        custom_strats = [cs for cs in custom_strats if cs["id"] not in _hidden]
        custom_ids    = {cs["id"] for cs in custom_strats}

        # Resolve display ID (custom strategies have an extra display key)
        display_id = rs.get("display_strategy_id", "") or rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id.startswith("custom_") and display_id not in custom_ids:
            display_id = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id in _hidden:
            display_id = STRATEGY_SCALE_OUT
        _cur_strat = [display_id]

        all_names = {
            k: v for k, v in STRATEGY_NAMES.items()
            # ORB/IVB is a time-of-day breakout engine, not a per-channel
            # strategy -- it has no signal/channel to attach to.
            if k not in _hidden and k != STRATEGY_ORB_FIXED
        }
        all_names.update({cs["id"]: cs["name"] for cs in custom_strats})

        # ── Top row: Strategy Parameters (half) + Channel Strategy (half) ────
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

          # ── Strategy Parameters card ─────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_strategy_params_card()

          # ── Channel Strategy card ─────────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_channel_strategy_card(engine, all_names, rs)

        # ── Global Parameters card (full width) ────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_global_parameters_card(rs)

        # ── EA Templates card (full width) ────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_ea_templates_card()

        # ── Risk Settings (full width, own internal 3+2 sub-card layout) ────────
        # Internal Engine Exposure and DPM (formerly on an "Active Strategy" card
        # here) moved into Risk Settings itself (2026-08-01) — strategy is
        # selected per-channel in Channel Strategy above, so a separate "Active
        # Strategy" card had no strategy-selection content of its own left to
        # justify existing as a distinct tab/card. render_risk_card lays out its
        # own five sub-cards (3 top row + 2 bottom row); no outer card here.
        render_risk_card("w-full")

        # ── Quick comparison table ─────────────────────────────────────────────
        compare_container = ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-2")

        def _draw_compare():
            compare_container.clear()
            fresh_customs = db_module.get_custom_strategies()
            _hidden_now   = _get_hidden_strategies()
            fresh_customs = [cs for cs in fresh_customs if cs["id"] not in _hidden_now]

            with compare_container:
                ui.label("Quick comparison").classes(
                    "text-sm font-bold text-gray-200 mb-3"
                )
                all_strat_nm = {
                    **{k: v for k, v in STRATEGY_NAMES.items() if k not in _hidden_now},
                    **{cs["id"]: cs["name"] for cs in fresh_customs},
                }
                active_strat = _cur_strat[0]

                # Build lookup for custom strategy comparison rows
                custom_cmp: dict[str, dict] = {}
                for cs in fresh_customs:
                    rules = json.loads(cs.get("rules_json") or "{}")
                    custom_cmp[cs["id"]] = {
                        "Partial closes": rules.get("partial_closes", "—"),
                        "SL moves to BE": rules.get("sl_moves_to_be", "—"),
                        "Max upside":     rules.get("max_upside", "—"),
                        "Risk after TP1": rules.get("risk_after_be", "—"),
                        "Signal quality filter": rules.get("signal_quality_filter", "—"),
                        "Best market":    rules.get("best_market", "—"),
                    }

                # Single shared confirmation dialog
                _pending: dict = {"sid": None, "sname": ""}
                with ui.dialog() as del_dialog, ui.card().classes(
                    "bg-gray-800 p-5 rounded-lg"
                ):
                    del_name_lbl = ui.label("").classes(
                        "text-gray-200 font-semibold mb-1"
                    )
                    ui.label("This cannot be undone.").classes(
                        "text-xs text-gray-400 mb-4"
                    )
                    with ui.row().classes("gap-2"):
                        def _do_del():
                            sid   = _pending["sid"]
                            sname = _pending["sname"]
                            if not sid:
                                return
                            if sid.startswith("custom_"):
                                db_module.delete_custom_strategy(sid)
                            else:
                                _hide_builtin_strategy(sid)
                            del_dialog.close()
                            ui.notify(f"Strategy '{sname}' deleted", type="warning")
                            _refresh()
                        ui.button(
                            "Delete", icon="delete", on_click=_do_del,
                        ).classes("bg-red-700 text-white text-sm px-3 py-1")
                        ui.button(
                            "Cancel", on_click=del_dialog.close,
                        ).classes("bg-gray-700 text-white text-sm px-3 py-1")

                def _render_table(strats: list, label: str) -> None:
                    """Render one comparison grid for the given strategy list."""
                    if not strats:
                        return
                    ui.label(label).classes("text-xs font-semibold text-gray-500 mt-3 mb-1")
                    cols = "140px " + " ".join(["1fr"] * len(strats))
                    with ui.element("div").style(
                        f"display:grid;grid-template-columns:{cols};"
                        "border:1px solid #374151;border-radius:6px;overflow:hidden;"
                    ):
                        # Header row — row-label cell
                        ui.element("div").style("padding:8px 10px;background:#1e2433;")

                        # Header row — one cell per strategy
                        for strat in strats:
                            is_active    = (strat == active_strat)
                            is_deletable = strat not in _PROTECTED_STRATS
                            name         = all_strat_nm.get(strat, strat)
                            col_bg       = "background:#1e3a52;" if is_active else "background:#1e2433;"
                            col_fg       = "color:#38bdf8;" if is_active else "color:#9ca3af;"
                            cell_base    = (
                                f"padding:8px 10px;font-size:12px;font-weight:600;"
                                f"border-left:1px solid #374151;{col_bg}"
                            )

                            if is_deletable:
                                with ui.element("div").style(
                                    cell_base + "display:flex;align-items:center;"
                                    "justify-content:space-between;gap:4px;"
                                ):
                                    ui.label(name).style(
                                        f"font-size:12px;font-weight:600;{col_fg}"
                                        "margin:0;overflow:hidden;text-overflow:ellipsis;"
                                        "white-space:nowrap;min-width:0;"
                                    )
                                    def _open_del(s=strat, n=name):
                                        _pending["sid"]   = s
                                        _pending["sname"] = n
                                        del_name_lbl.text = f"Delete '{n}'?"
                                        del_dialog.open()
                                    ui.button(
                                        icon="delete_outline", on_click=_open_del,
                                    ).props("dense flat round size=xs").classes(
                                        "text-red-500 shrink-0"
                                    ).tooltip(f"Delete '{name}'")
                            else:
                                ui.label(name).style(cell_base + col_fg)

                        # Data rows
                        for i, (row_label, row_data) in enumerate(_COMPARE_ROWS):
                            bg = "#111827" if i % 2 == 0 else "#0f1117"
                            ui.label(row_label).style(
                                f"padding:8px 10px;background:{bg};"
                                "font-size:11px;color:#6b7280;font-weight:600;"
                                "border-top:1px solid #374151;"
                            )
                            for strat in strats:
                                is_active = (strat == active_strat)
                                val = (
                                    custom_cmp.get(strat, {}).get(row_label, "—")
                                    if strat.startswith("custom_")
                                    else row_data.get(strat, "—")
                                )
                                if callable(val):
                                    val = val()
                                ui.label(val).style(
                                    f"padding:8px 10px;background:{bg};"
                                    "font-size:11px;font-family:monospace;"
                                    "border-left:1px solid #374151;"
                                    "border-top:1px solid #374151;"
                                    + ("color:#93c5fd;" if is_active else "color:#e5e7eb;")
                                )

                # Table 1 — core strategies
                g1 = [s for s in _COMPARE_GROUP_1 if s not in _hidden_now]
                _render_table(g1, "Core strategies")

                # Table 2 — advanced / specialised strategies + custom
                g2 = [s for s in _COMPARE_GROUP_2 if s not in _hidden_now]
                g2 += [cs["id"] for cs in fresh_customs]
                _render_table(g2, "Advanced & specialised strategies")

        _draw_compare()

    _refresh()


# ── TG Signals ─────────────────────────────────────────────────────────────────

def _render_tg_signals(engine):
    container = ui.column().classes("w-full gap-1")

    async def refresh():
        container.clear()
        sigs = await db_module.to_db_thread(engine.get_tg_signals, 50)
        with container:
            if not sigs:
                ui.label("No Telegram signals detected yet.").classes(
                    "text-gray-500 italic p-4"
                )
                return

            # Header row
            with ui.row().classes(
                "w-full items-center gap-2 px-3 py-1 text-xs text-gray-500 font-semibold "
                "uppercase tracking-wider border-b border-gray-700"
            ):
                ui.label("Time").classes("w-24 shrink-0")
                ui.label("Channel").classes("w-32 shrink-0 truncate")
                ui.label("Dir").classes("w-14 shrink-0 text-center")
                ui.label("Entry").classes("w-24 shrink-0 text-right")
                ui.label("SL").classes("w-20 shrink-0 text-right")
                ui.label("TPs").classes("flex-1 min-w-0")
                ui.label("Status").classes("w-36 shrink-0 text-center")
                ui.label("").classes("w-36 shrink-0")   # Execute + Delete columns

            for s in sigs:
                sig_id    = s.get("id")
                direction = s.get("direction", "?")
                tp_str    = "  ".join(
                    f"TP{i}:{s.get(f'tp{i}'):.0f}"
                    for i in range(1, 9) if s.get(f"tp{i}")
                )
                dir_cls = "text-green-400" if direction == "BUY" else "text-red-400"
                status  = s.get("status", "?")

                async def delete_sig(row_id=sig_id):
                    try:
                        with db_module.db() as conn:
                            conn.execute(
                                "DELETE FROM vantage_tg_signals WHERE id=?", (row_id,)
                            )
                        await refresh()
                    except Exception as ex:
                        ui.notify(str(ex), type="negative")

                # Use the actual Telegram message send time; fall back to import time
                display_ts = s.get("message_ts") or s.get("parsed_at")
                status_color = {
                    "new":                  "blue",
                    "activated":            "green",
                    "historical":           "grey",
                    "pending":              "orange",
                    "instant_pending":      "amber",
                    "instant_activated":    "green",
                    "instant_failed":       "red",
                    "followup_applied":     "teal",
                    "unsupported_currency": "orange",
                }.get(status, "grey")

                with ui.row().classes(
                    "w-full items-center gap-2 px-3 py-1.5 text-xs "
                    "bg-gray-800 rounded hover:bg-gray-750"
                ).style("background:#1e2433"):
                    ui.label(_uk(display_ts)).classes(
                        "w-24 shrink-0 font-mono text-gray-400"
                    )
                    ui.label(
                        s.get("group_name") or s.get("group_id", "—")
                    ).classes("w-32 shrink-0 truncate text-gray-300")
                    ui.label(direction).classes(f"w-14 shrink-0 text-center font-bold {dir_cls}")
                    _entry_lo = s.get("entry_low")
                    _entry_hi = s.get("entry_high")
                    _sl_val   = s.get("stop_loss")
                    ui.label(
                        f"{float(_entry_lo):.0f}–{float(_entry_hi):.0f}"
                        if _entry_lo and _entry_hi else "—"
                    ).classes("w-24 shrink-0 text-right font-mono text-gray-200")
                    ui.label(
                        f"{float(_sl_val):.0f}" if _sl_val else "—"
                    ).classes("w-20 shrink-0 text-right font-mono text-red-400")
                    ui.label(tp_str or "—").classes(
                        "flex-1 min-w-0 font-mono text-green-400 truncate"
                    )
                    ui.badge(status, color=status_color).classes(
                        "w-36 px-2 shrink-0 text-center"
                    )
                    exec_btn_ref = [None]

                    async def execute_sig(row_id=sig_id, sig_data=s, _btn=exec_btn_ref):
                        if sig_data.get("status") == "unsupported_currency":
                            ui.notify("Cannot execute — signal is not XAUUSD", type="warning")
                            return
                        if _btn[0]:
                            _btn[0].props("loading=true disabled=true")
                        try:
                            new_sig = engine.create_signal(
                                source_name=sig_data.get("group_name") or "TG",
                                direction=sig_data.get("direction", "BUY"),
                                entry_low=float(sig_data.get("entry_low") or 0),
                                entry_high=float(sig_data.get("entry_high") or 0),
                                stop_loss=float(sig_data.get("stop_loss") or 0),
                                tp1=sig_data.get("tp1") or None,
                                tp2=sig_data.get("tp2") or None,
                                tp3=sig_data.get("tp3") or None,
                                tp4=sig_data.get("tp4") or None,
                                tp5=sig_data.get("tp5") or None,
                                tp6=sig_data.get("tp6") or None,
                                tp7=sig_data.get("tp7") or None,
                                tp8=sig_data.get("tp8") or None,
                            )
                            result = await engine.open_trade_from_signal(new_sig["signal_id"])
                            ui.notify(
                                f"Trade opened @ {result['entry_price']}", type="positive"
                            )
                            await refresh()
                        except Exception as ex:
                            ui.notify(str(ex), type="negative")
                        finally:
                            if _btn[0]:
                                try:
                                    _btn[0].props(remove="loading disabled")
                                except Exception:
                                    pass

                    exec_btn_ref[0] = ui.button(
                        "Execute", on_click=execute_sig,
                    ).classes(
                        "shrink-0 bg-green-800 text-green-300 text-xs px-2 py-0.5 "
                        "hover:bg-green-600"
                    ).props("dense flat")
                    ui.button(
                        "Delete", on_click=delete_sig,
                    ).classes(
                        "shrink-0 bg-red-900 text-red-300 text-xs px-2 py-0.5 "
                        "hover:bg-red-700"
                    ).props("dense flat")

    ui.timer(3.0, refresh)
    asyncio.ensure_future(refresh())
