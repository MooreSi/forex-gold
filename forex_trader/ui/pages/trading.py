"""Trading page — active positions, signal entry, strategy, Telegram signals."""

import asyncio
import json
import logging
import time as _time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

from nicegui import ui

import forex_trader.config as cfg_module
from forex_trader.core import ai_provider
from forex_trader.core import database as db_module
from forex_trader.core.models import (
    STRATEGY_NAMES, STRATEGY_DESCRIPTIONS, STRATEGY_SCALE_OUT, STRATEGY_ORB_FIXED,
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
        t_active   = ui.tab("Active Trades")
        t_pending  = ui.tab("Pending Signals")
        t_signal   = ui.tab("Limit Order")
        t_market   = ui.tab("Market Order")
        t_strategy = ui.tab("Strategy")
        t_tg_sigs  = ui.tab("TG Signals")
        t_orb      = ui.tab("ORB/IVB Report")
        t_schedule = ui.tab("Schedule")

    with ui.tab_panels(trade_tabs, value=t_active).classes("bg-gray-900 p-4"):

        with ui.tab_panel(t_active):
            _render_active_trades(engine)

        with ui.tab_panel(t_pending):
            _render_pending_signals(engine)

        with ui.tab_panel(t_signal):
            _render_signal_entry(engine)

        with ui.tab_panel(t_market):
            _render_market_order_form(engine)

        with ui.tab_panel(t_strategy):
            _render_strategy(engine)

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

        async def _validate_and_get():
            errors = validate_signal(
                direction.value, entry_low.value or 0, entry_high.value or 0,
                stop_loss.value or 0,
                tp1.value or None, tp2.value or None, tp3.value or None,
                tp4.value or None, tp5.value or None,
                tp6.value or None, tp7.value or None, tp8.value or None,
            )
            if errors:
                status_lbl.text = " | ".join(errors)
                return None
            return engine.create_signal(
                source_name="Manual", direction=direction.value,
                entry_low=entry_low.value or 0, entry_high=entry_high.value or 0,
                stop_loss=stop_loss.value or 0,
                tp1=tp1.value or None, tp2=tp2.value or None, tp3=tp3.value or None,
                tp4=tp4.value or None, tp5=tp5.value or None,
                tp6=tp6.value or None, tp7=tp7.value or None, tp8=tp8.value or None,
                lot_size=lot_size.value if lot_size.value else None,
                notes=notes.value,
            )

        async def submit():
            status_lbl.text = ""
            try:
                sig = await _validate_and_get()
                if sig:
                    ui.notify(
                        f"Signal saved — see Pending Signals tab to execute it.",
                        type="positive",
                    )
                    asyncio.create_task(_background_commentary(engine, sig["signal_id"]))
            except Exception as e:
                status_lbl.text = str(e)

        async def submit_and_open():
            status_lbl.text = ""
            try:
                sig = await _validate_and_get()
                if not sig:
                    return
                result = await engine.open_trade_from_signal(
                    sig["signal_id"], lot_size.value if lot_size.value else None
                )
                ui.notify(
                    f"Trade opened @ {result['entry_price']}", type="positive"
                )
            except Exception as e:
                status_lbl.text = str(e)
                ui.notify(str(e), type="negative")

        with ui.row().classes("gap-2 mt-4"):
            ui.button("Save Signal", on_click=submit).classes("bg-blue-700 text-white px-4 py-2")
            ui.button("Save & Open Trade", on_click=submit_and_open).classes(
                "bg-green-700 text-white px-4 py-2"
            )

    ui.label(
        "Saved signals appear in the Pending Signals tab where you can review them and open a trade."
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
    """London open ORB/IVB report — trades the breakout of the last hour
    before London opens, evaluated within the first 15 minutes of London,
    volume profile, reload-zone entry setup, manual execute, and an
    auto-execute-every-morning toggle (places a genuine EA pending order)."""
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
                    "No ORB report available yet — this builds from the last hour "
                    "before London opens and is only available during the first 15 "
                    "minutes after London opens."
                ).classes("text-gray-500 text-sm italic p-4")
                return

            direction = report.get("direction", "inside")
            _label = {"bullish": ("BREAKOUT — BULLISH", "text-green-400"),
                      "bearish": ("BREAKOUT — BEARISH", "text-red-400"),
                      "inside": ("INSIDE RANGE", "text-gray-400")}
            status_txt, status_col = _label.get(direction, ("—", "text-gray-400"))

            with ui.card().classes("w-full max-w-3xl bg-gray-800 p-6 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.icon("candlestick_chart").classes("text-amber-400 text-xl")
                    ui.label("London Open — ORB/IVB Report").classes("text-lg font-bold text-yellow-300")

                with ui.row().classes("items-center gap-4 mb-3"):
                    ui.label(f"Current price: ${float(report.get('current_price', 0) or 0):.2f}").classes(
                        "text-sm text-gray-300 bg-gray-700 px-3 py-1 rounded"
                    )
                    ui.label(status_txt).classes(f"text-sm font-bold {status_col}")

                # Pre-London reference range (last hour before London open) + volume profile
                with ui.grid(columns=4).classes("w-full text-sm gap-2 mb-2"):
                    _stat_cell(
                        "PRE-LONDON RANGE",
                        f"${report['range_low']:.2f} – ${report['range_high']:.2f}  "
                        f"({report['range_height']:.1f} pts)",
                    )
                    _stat_cell("POC", f"${report['poc']:.2f}")
                    _stat_cell("VALUE AREA", f"${report['val']:.2f} – ${report['vah']:.2f}")
                    _stat_cell("WINDOW", "London open + 15min")

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

                # Reload-zone entry/exit setup, if a breakout has happened
                if direction in ("bullish", "bearish"):
                    rr = report.get("rr")
                    n = report.get("target_sample_n", 0)
                    is_default = report.get("target_is_default", True)
                    confidence_note = (
                        f"default 2.0x multiple — only {n} clean-breakout day(s) measured so far"
                        if is_default else
                        f"empirically measured from the last {n} clean-breakout days on this account"
                    )

                    ui.label("Reload Zone Setup").classes(
                        "text-xs font-semibold text-amber-300 uppercase tracking-wide mt-2 mb-1"
                    )
                    with ui.grid(columns=4).classes("w-full text-sm gap-2 mb-1"):
                        _stat_cell(
                            "ENTRY ZONE",
                            f"${report['entry_zone_low']:.2f} – ${report['entry_zone_high']:.2f}",
                        )
                        _stat_cell("STOP", f"${report['stop']:.2f}", "text-red-400")
                        _stat_cell("TARGET", f"${report['target']:.2f}", "text-green-400")
                        _stat_cell("R:R", f"{rr:.2f}:1" if rr else "—", "text-amber-300")
                    ui.label(
                        f"Target = breakout level x {report.get('target_multiple', 2.0):.2f} of the "
                        f"Asian range height ({confidence_note}). Stop = breakout level ± "
                        f"{report.get('sl_range_pct', 0.50) * 100:.0f}% of the Asian range height."
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
                        f"Execute {mt5_direction} at Market — ORB/IVB Setup",
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
                else:
                    ui.label(
                        "No breakout yet — the manual Execute button appears once price "
                        "clears the Asian range."
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

# ── Strategy builder (Claude) ─────────────────────────────────────────────────

_STRATEGY_BUILDER_SYSTEM = (
    "You are a professional XAUUSD (gold) trading strategy designer. "
    "The user will describe a trading strategy in plain English. "
    "Interpret their intent and produce a formal strategy definition. "
    "Respond ONLY with a single minified JSON object — no markdown fences, no extra text — "
    "matching this exact schema:\n"
    '{"name":"Short Name (2-5 words)",'
    '"description":"Full markdown description with ## header, bullet points, '
    'concrete XAUUSD example with entry/SL/TP prices, and a Best for: section",'
    '"base_strategy":"scale_out|be_runner|trail_stop|protected_scale|conservative",'
    '"compare":{'
    '"partial_closes":"one line",'
    '"sl_moves_to_be":"one line",'
    '"max_upside":"one line",'
    '"risk_after_be":"one line",'
    '"best_market":"one line"}}\n'
    "base_strategy — choose the closest built-in execution model:\n"
    "  scale_out: 20% partial closes at each TP, SL to breakeven after TP1\n"
    "  be_runner: no partial closes, SL steps to previous TP after each level\n"
    "  trail_stop: trailing stop activates after TP1, no partial closes\n"
    "  protected_scale: hold through TP1+TP2 (SL to BE at TP2), 20% closes from TP3\n"
    "  conservative: 5-pt SL and 3-pt TP1 from fill price; 80% close at TP1 (SL → fill/breakeven); 20% runner with 3-pt trailing stop; ignores signal SL/TP levels entirely\n"
    "Description: 150-300 words, realistic XAUUSD prices "
    "(entries ~2500-2600, TPs 10-50 points apart, SL 20-80 points away)."
)


async def _call_strategy_builder(description: str, cfg: dict) -> dict:
    """Call the configured AI provider to convert natural language into a structured strategy definition."""
    from forex_trader.core import ai_provider as _aip
    raw = await _aip.complete(cfg, _STRATEGY_BUILDER_SYSTEM, description, max_tokens=1500, timeout=60)
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return json.loads(raw)

_STRAT_ORDER = [_SO, _BE, _TS, _PS, _CO, _NSS, _CT, _SR, _SC, _RVR, _AR, _AR2]
_PROTECTED_STRATS = frozenset({_SO, _BE, _TS, _PS, _CO, _SR, _SC, _RVR, _AR, _AR2})


def _render_schedule():
    """Trading Schedule tab — per-day, per-window profit-target discipline
    cap on AUTOMATED order execution (manual orders are always exempt).
    Signal generation and Telegram ingestion are never affected -- see
    core_trading_schedule.py / core_signal_resolution.py for the gate this
    UI configures."""
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
        "profit target is met trading pauses until the next window. Signal generation "
        "and Telegram ingestion keep running regardless -- this only blocks the final "
        "order-placement step, and only for automated (not manual) orders."
    ).classes("text-xs text-gray-500 mb-3")

    _day_widgets: dict[str, list[dict]] = {}

    with ui.column().classes("w-full gap-2"):
        for day in sched.DAY_NAMES:
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label(day.title()).classes("font-bold text-yellow-300 text-sm mb-1")
                blocks = schedule[day]
                _day_widgets[day] = []
                for i, block in enumerate(blocks):
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
                        _day_widgets[day].append(
                            {"enabled": en, "start": start, "end": end, "target": target}
                        )

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
                    blocks.append({
                        "enabled": bool(w["enabled"].value),
                        "start":   start_val,
                        "end":     end_val,
                        "target":  float(w["target"].value or 0),
                    })
                new_schedule[day] = blocks
        except Exception as e:
            ui.notify(f"Invalid time — use 24-hour HH:MM (e.g. 09:00): {e}", type="negative")
            return
        sched.set_trading_schedule(new_schedule)
        sched.set_trading_schedule_enabled(bool(master_chk.value))
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
        _SO:  "Yes — 20% per TP",
        _BE:  "No",
        _TS:  "No",
        _PS:  "Yes — 20% from TP3",
        _CO:  "80% at TP1 (+3 pts from fill) · remainder trails via 3-pt stop",
        _NSS: "20% TP1 · 20% TP3 · rest at last TP (max TP8)",
        _CT:  "5% TP1 · 30% TP2 · 20% TP3 · 40% TP4 · 5% TP5 · rest at TP6",
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
        _CT:  "At TP2 (+10 pts from fill)",
        _SR:  "At TP2 (+4 pts from fill) — SL moves to fill price (entry)",
        _SC:  "After TP1 → entry (BE); after TP2+ → trails to previous TP price",
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
        _CT:  "+35 pts from fill price (TP6 fixed target)",
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
        _BE:  "ADX > 25 required — falls back to Scale Out in ranging markets",
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
        _BE:  "Strong trend (ADX > 25)",
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
    from forex_trader.core.models import STRATEGY_NAMES

    with ui.row().classes("items-center gap-2 mb-1"):
        ui.label("Channel Strategy").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Assign a strategy per channel. Auto lets Claude evaluate market "
            "conditions and update the recommendation every 30 min."
        )

    channels = _csdb.get_all_channel_strategy_settings()

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
    Strategy Parameters: live-editable SL/TP point values for the
    fixed-parameter strategies (Conservative, Scalp Runner, Reversal Runner,
    Adaptive Runner, Adaptive Runner 2), plus a small named-template
    library to save/reapply a parameter set later. A change here applies
    to the next trade opened under that strategy -- no restart, no code
    change. Mirrors a third-party EA's "Settings Templates" panel
    investigated 2026-07-22; see core_strategy_params.py's module
    docstring for why this needed no MQL5 changes at all (these five
    strategies are already fully resolved to concrete SL/TP prices by
    Python before any EA sees the trade).
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
    ).classes("w-56 mb-2").props("dense outlined").on_value_change(_on_strategy_change)

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


def _render_ea_templates_card() -> None:
    """
    EA Templates: complete, self-contained, EA-managed trade-management
    definitions (Grid vs Single, TP/SL visibility, trailing method,
    breakeven rule, cancel-pending-siblings, profit harvesting) -- a
    channel can be assigned a saved template in the Channel Strategy card
    below in place of a built-in strategy. Unlike Strategy Parameters
    above (which only retunes existing Python-managed strategies), a
    template fully replaces strategy dispatch and the EA manages the
    trade end-to-end -- every field here is sent fresh on each open, so
    changing a template's values never needs an EA recompile. See
    core_ea_templates.py's module docstring.
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

    def _draw_body() -> None:
        body.clear()
        live = (et.get_ea_template(state["name"]) if state["name"] else None) or dict(et.DEFAULTS)
        fields.clear()
        with body:
            with ui.row().classes("items-center gap-2 mb-2"):
                existing = et.list_ea_templates()
                load_opts = {"": "— New Template —"}
                load_opts.update({t["name"]: t["name"] for t in existing})
                ui.select(
                    load_opts, value=state["name"] or "", label="Load",
                ).classes("w-56").props("dense outlined").on_value_change(
                    lambda e: _load(e.value or None)
                )
                name_input = ui.input(
                    "Template name", value=state["name"] or "",
                ).classes("w-56").props("dense outlined")

            ui.label("Strategy").classes("text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1")
            with ui.grid(columns=2).classes("w-full gap-3 mb-2"):
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["tg_cmd_enabled"] = ui.switch(
                        "TG CMD", value=bool(live["tg_cmd_enabled"]),
                    ).classes("text-sm")
                    ui.label(
                        "Logic Keywords triggers (CLOSE ALL / RISK FREE-BE / TP HIT) "
                        "apply to trades opened under this template."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["harvest_enabled"] = ui.switch(
                        "Harvest", value=bool(live["harvest_enabled"]),
                    ).classes("text-sm")
                    fields["harvest_threshold"] = ui.number(
                        "Profit threshold ($)", value=float(live["harvest_threshold"]),
                        step=5.0,
                    ).classes("w-full mt-1").props("dense outlined")
                    ui.label(
                        "Auto-close and bank profit once floating P&L reaches this "
                        "amount, independent of any TP level."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Mode").classes("text-sm")
                    fields["mode"] = ui.select(
                        {c: c.title() for c in et.MODE_CHOICES}, value=live["mode"],
                    ).classes("w-full").props("dense outlined")
                    fields["grid_step_pts"] = ui.number(
                        "Grid step (pt)", value=float(live["grid_step_pts"]), step=1.0,
                    ).classes("w-full mt-1").props("dense outlined")
                    fields["grid_legs"] = ui.number(
                        "Grid legs", value=int(live["grid_legs"]), step=1, min=2, max=10,
                    ).classes("w-full mt-1").props("dense outlined")
                    ui.label(
                        "Single: one entry per signal. Grid: stages multiple entries "
                        "step pts apart to average into the position."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("TP/SL").classes("text-sm")
                    fields["tpsl_mode"] = ui.select(
                        {c: c.title() for c in et.TPSL_MODE_CHOICES}, value=live["tpsl_mode"],
                    ).classes("w-full").props("dense outlined")
                    ui.label(
                        "Off: no target, rides with no exit. On: real broker-side "
                        "SL/TP. Stealth: tracked internally, closes at market when "
                        "hit -- never written to the order ticket."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Anchor").classes("text-sm")
                    fields["anchor"] = ui.select(
                        {c: c.title() for c in et.ANCHOR_CHOICES}, value=live["anchor"],
                    ).classes("w-full").props("dense outlined")
                    ui.label(
                        "Unified: every leg trails/BEs off the original entry price. "
                        "Distributed: each leg manages its own reference independently."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("Trail").classes("text-sm")
                    fields["trail_mode"] = ui.select(
                        {c: c.title() for c in et.TRAIL_MODE_CHOICES}, value=live["trail_mode"],
                    ).classes("w-full").props("dense outlined")
                    ui.label(
                        "Candle: trails to recent candle highs/lows. Step: fixed-"
                        "point trailing. Fractal: trails to swing-pivot fractals. "
                        "TP: trails up to each TP price as it's hit."
                    ).classes("text-xs text-gray-500 mt-1")

            ui.label("Triggers").classes("text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1")
            with ui.grid(columns=2).classes("w-full gap-3 mb-2"):
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    ui.label("BE Mode").classes("text-sm")
                    fields["be_mode"] = ui.select(
                        {c: c.replace("_", " ").title() for c in et.BE_MODE_CHOICES},
                        value=live["be_mode"],
                    ).classes("w-full").props("dense outlined")
                    fields["be_buffer_pts"] = ui.number(
                        "BE buffer (pt)", value=float(live["be_buffer_pts"]), step=0.5,
                    ).classes("w-full mt-1").props("dense outlined")
                    ui.label(
                        "Entry: SL moves exactly to entry price. Entry+Buffer: locks "
                        "in buffer pts of profit instead of dead-even."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["be_trigger"] = ui.number(
                        "BE Trigger (TP#)", value=int(live["be_trigger"]), step=1, min=1, max=8,
                    ).classes("w-full").props("dense outlined")
                    ui.label(
                        "Which TP level arms the breakeven move."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["cancel_pending"] = ui.switch(
                        "Cancel Pending", value=bool(live["cancel_pending"]),
                    ).classes("text-sm")
                    ui.label(
                        "When one leg of a multi-order signal fills, cancel the "
                        "other still-resting pending legs."
                    ).classes("text-xs text-gray-500 mt-1")

                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    fields["sig_guard"] = ui.switch(
                        "Sig Guard", value=bool(live["sig_guard"]),
                    ).classes("text-sm")
                    ui.label(
                        "Block a new template-managed trade for the same channel/"
                        "direction while one is already open."
                    ).classes("text-xs text-gray-500 mt-1")

            with ui.row().classes("gap-2 mt-1"):
                ui.button(
                    "Save Template", on_click=lambda: _save(name_input),
                ).classes("text-xs bg-green-800 text-white").props("dense")
                if state["name"]:
                    ui.button(
                        "Delete", on_click=lambda: _delete(state["name"]),
                    ).classes("text-xs bg-red-900 text-white").props("dense")
                ui.button(
                    "New", on_click=lambda: _load(None),
                ).classes("text-xs").props("dense outline")

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
            if k not in _hidden
        }
        all_names.update({cs["id"]: cs["name"] for cs in custom_strats})

        def _get_desc(key: str) -> str:
            if key in STRATEGY_DESCRIPTIONS:
                return STRATEGY_DESCRIPTIONS[key]
            cs = next((c for c in custom_strats if c["id"] == key), None)
            return (cs or {}).get("description", "")

        # ── Top row: Strategy Parameters (half) + Channel Strategy (half) ────
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

          # ── Strategy Parameters card ─────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_strategy_params_card()

          # ── Channel Strategy card ─────────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_channel_strategy_card(engine, all_names, rs)

        # ── EA Templates card (full width) ────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_ea_templates_card()

        # ── 3-card row ────────────────────────────────────────────────────────
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):

            # ── Card 1: Active Strategy ───────────────────────────────────────
            with ui.card().classes("bg-gray-800 p-4 rounded-lg shrink-0 w-72"):
                ui.label("Active Strategy").classes("text-base font-bold text-yellow-300 mb-1")
                ui.label(
                    "Global default — overridden per-channel above when a channel has "
                    "a manual or Auto assignment set."
                ).classes("text-xs text-gray-500 italic mb-3")

                with ui.column().classes("gap-0 w-full"):
                    ui.label("Global Strategy").classes("text-xs text-gray-400 font-medium mb-0.5")
                    sel = ui.select(all_names, value=_cur_strat[0]).classes("w-full")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trailing Stop SL (pts)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Trailing Stop strategy only. "
                            "Initial stop-loss distance from entry price — gives the trade breathing room "
                            "before the trail activates at TP1."
                        )
                    trail_stop_sl = ui.number(
                        value=float(rs.get("trail_stop_sl_pts", 5.0)),
                        min=0.5, step=0.5, format="%.1f",
                    ).classes("w-full")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trailing Stop Distance (pts)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Trailing Stop strategy only. "
                            "TP1–TP8 are set at 1×–8× this distance from entry. "
                            "After TP1 is reached, the SL trails at this distance behind price."
                        )
                    trail_dist = ui.number(
                        value=float(rs.get("trailing_stop_distance", 5.0)),
                        min=0.1, step=0.5, format="%.1f",
                    ).classes("w-full")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Fixed Lot Size (0 = risk-based auto)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Override the risk-calculated lot size. Set to 0 to use automatic sizing."
                        )
                    lot_override = ui.number(
                        value=float(rs.get("strategy_lot_size", 0.0)),
                        min=0.0, step=0.01, format="%.2f",
                    ).classes("w-full")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Profit Take Target ($ cumulative, 0 = disabled)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Close automatically when cumulative profit (partial closes already "
                            "taken + unrealised P&L on remaining lots) reaches this USD amount. "
                            "Applies to all strategies including DPM. Set to 0 to disable."
                        )
                    tp_at_usd = ui.number(
                        value=float(rs.get("profit_close_usd", 0.0) or 0.0),
                        min=0.0, step=1.0, format="%.2f",
                    ).classes("w-full")

                ui.separator().classes("my-2")

                with ui.column().classes("gap-0 w-full"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("ATR Collapse Threshold (0.0–1.0)").classes("text-xs text-gray-400 font-medium")
                        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                            "Suppresses signals when current ATR falls below this fraction of the recent "
                            "ATR baseline. 0.65 = block when volatility drops to 65% or below of normal. "
                            "Set to 0 to disable. Prevents trading in dead-market chop (Asian gaps, "
                            "pre-news consolidation) where spread costs eliminate edge."
                        )
                    atr_collapse_thresh = ui.number(
                        value=float(rs.get("atr_collapse_threshold", 0.65) or 0.65),
                        min=0.0, max=1.0, step=0.05, format="%.2f",
                    ).classes("w-full")

                kelly_enabled = ui.checkbox(
                    "Kelly Criterion Sizing",
                    value=bool(rs.get("kelly_sizing_enabled", 0)),
                ).classes("text-sm text-gray-300 mt-1")
                kelly_enabled.tooltip(
                    "Adjusts live-execution lot size using half-Kelly Criterion "
                    "based on rolling 50-trade win rate and R:R. "
                    "Multiplier is clamped to [0.75x, 1.25x] — modest adjustment only. "
                    "Requires ≥20 closed trades to activate."
                )

                def save_strategy():
                    try:
                        key = sel.value
                        if key.startswith("custom_"):
                            cs_match = next((c for c in custom_strats if c["id"] == key), None)
                            exec_key = cs_match["base_strategy"] if cs_match else STRATEGY_SCALE_OUT
                        else:
                            exec_key = key
                        db_module.update_risk_settings({
                            "trade_strategy":          exec_key,
                            "display_strategy_id":     key,
                            "trail_stop_sl_pts":       float(trail_stop_sl.value or 5.0),
                            "trailing_stop_distance":  float(trail_dist.value or 3.0),
                            "strategy_lot_size":       lot_override.value,
                            "profit_close_usd":        float(tp_at_usd.value or 0),
                            "atr_collapse_threshold":  float(atr_collapse_thresh.value or 0.65),
                            "kelly_sizing_enabled":    int(kelly_enabled.value),
                        })
                        _cur_strat[0] = key
                        _refresh_desc(key)
                        _draw_compare()
                        ui.notify(f"Strategy saved: {all_names.get(key, key)}", type="positive")
                    except Exception as ex:
                        ui.notify(str(ex), type="negative")

                ui.button("Save Strategy", on_click=save_strategy).classes(
                    "bg-blue-700 text-white mt-3 px-4 py-2 text-sm"
                )

                # Auto-Execution moved to Parsing > Logic Keywords tab (2026-07-23).

                # ── Dynamic Position Management ───────────────────────────────
                ui.separator().classes("my-3")
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("psychology").classes("text-blue-400 text-base")
                    ui.label("Dynamic Position Management").classes(
                        "text-sm font-semibold text-blue-300"
                    )
                    dpm_enabled_val = bool(rs.get("dpm_enabled", 0))
                    dpm_badge = ui.badge(
                        "DPM ON" if dpm_enabled_val else "DPM OFF",
                        color="blue" if dpm_enabled_val else "grey",
                    )

                dpm_chk = ui.checkbox(
                    "Hand off to adaptive management",
                    value=dpm_enabled_val,
                ).classes("text-sm text-gray-200")

                def _dpm_toggle(e):
                    db_module.update_risk_settings({"dpm_enabled": 1 if e.value else 0})
                    dpm_badge.props(f"color={'blue' if e.value else 'grey'}")
                    dpm_badge.text = "DPM ON" if e.value else "DPM OFF"
                    ui.notify(
                        "DPM enabled — strategy control handed off" if e.value
                        else "DPM disabled — strategy selection restored",
                        type="positive" if e.value else "info",
                    )

                dpm_chk.on_value_change(_dpm_toggle)

                # ── DPM Profit Take ───────────────────────────────────────────
                with ui.row().classes("items-end gap-2 mt-2 w-full"):
                    with ui.column().classes("flex-1 gap-0"):
                        with ui.row().classes("items-center gap-1"):
                            ui.label("Profit Take ($)").classes(
                                "text-xs text-gray-400 font-medium"
                            )
                            ui.icon("info_outline", size="xs").classes(
                                "text-blue-400 cursor-help"
                            ).tooltip(
                                "Close the remaining position when cumulative profit — "
                                "partial closes already taken plus unrealised P&L on "
                                "remaining lots — reaches this amount.\n"
                                "Example: set $150 and DPM will keep managing the trade "
                                "through its normal TP levels until the running total "
                                "hits $150, then close everything.\n"
                                "0 = DPM decides entirely (no dollar cap)."
                            )
                        dpm_profit_inp = ui.number(
                            value=float(rs.get("profit_close_usd", 0.0) or 0.0),
                            min=0.0, step=5.0, format="%.2f",
                            placeholder="0 = DPM decides",
                        ).classes("w-full")

                    def _save_dpm_profit():
                        try:
                            val = max(0.0, float(dpm_profit_inp.value or 0))
                            db_module.update_risk_settings({"profit_close_usd": val})
                            if val > 0:
                                ui.notify(
                                    f"Profit take set to ${val:.2f} — DPM will close when "
                                    f"cumulative profit reaches this amount",
                                    type="positive",
                                )
                            else:
                                ui.notify(
                                    "Profit take cleared — DPM manages profit levels entirely",
                                    type="info",
                                )
                        except Exception as ex:
                            ui.notify(str(ex), type="negative")

                    ui.button("Set", on_click=_save_dpm_profit).classes(
                        "bg-blue-700 text-white px-3 text-xs"
                    ).style("height:30px; min-width:44px;")

                ui.label(
                    "Automatically adjusts trail distance, breakeven timing and "
                    "partial close size using ATR, session and momentum. "
                    "Set a Profit Take amount above to cap the cumulative target — "
                    "otherwise DPM decides entirely."
                ).classes("text-xs text-gray-400 mt-2 leading-relaxed")

                # Immediate Market Buy/Sell moved to Parsing > Logic Keywords tab (2026-07-23).

            # ── Card 1b: Risk Settings (linked to Active Strategy) ────────────
            _set_risk_lot = render_risk_card("bg-gray-800 p-4 rounded-lg shrink-0 w-72")
            # Reactively grey/ungrey risk-per-trade fields when lot_override changes.
            lot_override.on_value_change(
                lambda e: _set_risk_lot(float(e.value or 0))
            )

            # ── Card 2: Strategy Builder ──────────────────────────────────────
            with ui.card().classes("flex-1 bg-gray-800 p-4 rounded-lg min-w-72"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("auto_awesome").classes("text-purple-400 text-xl")
                    ui.label("Strategy Builder").classes(
                        "text-base font-bold text-purple-300"
                    )
                ui.label(
                    "Describe a strategy in plain English — AI will define it for you."
                ).classes("text-xs text-gray-400 mb-2")

                desc_input = ui.textarea(
                    label="Strategy description",
                    placeholder=(
                        "e.g. Take 30% at TP1, move SL to breakeven, "
                        "then hold the rest to TP3 with a trailing stop..."
                    ),
                ).classes("w-full").props("rows=3 autogrow")

                build_status  = ui.label("").classes("text-xs text-gray-400 mt-1")
                builder_output = ui.column().classes("w-full gap-2 mt-2")

                def _show_result(result: dict):
                    builder_output.clear()
                    with builder_output:
                        with ui.card().classes("w-full p-3 rounded").style(
                            "border:1px solid #6b21a8;background:#1a1128"
                        ):
                            name_inp = ui.input(
                                "Strategy name",
                                value=result.get("name", ""),
                            ).classes("w-full text-sm font-semibold")

                            with ui.expansion(
                                "Description (editable)", icon="description",
                            ).classes("w-full bg-gray-700 rounded text-xs mt-1"):
                                desc_ta = ui.textarea(
                                    value=result.get("description", ""),
                                ).classes("w-full font-mono text-xs").props("rows=6")

                            ui.label("Quick Comparison Rows").classes(
                                "text-xs text-gray-400 font-semibold mt-2"
                            )
                            cmp = result.get("compare", {})
                            inp_partial = ui.input(
                                "Partial closes",
                                value=cmp.get("partial_closes", ""),
                            ).classes("w-full text-xs")
                            inp_sl = ui.input(
                                "SL moves to BE",
                                value=cmp.get("sl_moves_to_be", ""),
                            ).classes("w-full text-xs")
                            inp_up = ui.input(
                                "Max upside",
                                value=cmp.get("max_upside", ""),
                            ).classes("w-full text-xs")
                            inp_risk = ui.input(
                                "Risk after BE",
                                value=cmp.get("risk_after_be", ""),
                            ).classes("w-full text-xs")
                            inp_mkt = ui.input(
                                "Best market",
                                value=cmp.get("best_market", ""),
                            ).classes("w-full text-xs")

                            base_key  = result.get("base_strategy", STRATEGY_SCALE_OUT)
                            base_name = STRATEGY_NAMES.get(base_key, base_key)
                            ui.label(f"Executes as: {base_name}").classes(
                                "text-xs text-gray-500 italic mt-1"
                            )

                            def _accept():
                                custom_id = f"custom_{_uuid.uuid4().hex[:8]}"
                                db_module.save_custom_strategy({
                                    "id":            custom_id,
                                    "name":          (name_inp.value or "").strip()
                                                     or "Custom Strategy",
                                    "description":   desc_ta.value.strip(),
                                    "base_strategy": base_key,
                                    "rules_json":    json.dumps({
                                        "partial_closes": inp_partial.value,
                                        "sl_moves_to_be": inp_sl.value,
                                        "max_upside":     inp_up.value,
                                        "risk_after_be":  inp_risk.value,
                                        "best_market":    inp_mkt.value,
                                    }),
                                    "created_at": _time.time(),
                                })
                                ui.notify(
                                    f"Strategy '{name_inp.value}' saved", type="positive"
                                )
                                _refresh()

                            def _discard():
                                builder_output.clear()
                                build_status.text = "Discarded."
                                build_status.classes(replace="text-xs text-gray-400 mt-1")

                            with ui.row().classes("gap-2 mt-3"):
                                ui.button(
                                    "Accept", icon="check_circle", on_click=_accept,
                                ).classes("bg-green-700 text-white text-sm px-3 py-1")
                                ui.button(
                                    "Discard", icon="delete_outline", on_click=_discard,
                                ).props("flat").classes(
                                    "text-red-400 text-sm px-3 py-1"
                                )

                async def _build():
                    text   = (desc_input.value or "").strip()
                    config = cfg_module.load()

                    if not text:
                        build_status.text = "Enter a strategy description first."
                        build_status.classes(replace="text-xs text-orange-400 mt-1")
                        return
                    if not ai_provider.is_configured(config):
                        build_status.text = (
                            "AI provider not configured — go to Settings > AI."
                        )
                        build_status.classes(replace="text-xs text-orange-400 mt-1")
                        return

                    build_btn.disable()
                    builder_output.clear()
                    build_status.text = "Building with AI..."
                    build_status.classes(replace="text-xs text-blue-400 mt-1")
                    try:
                        result = await _call_strategy_builder(text, config)
                        _show_result(result)
                        build_status.text = (
                            "Strategy generated — review and edit below, then Accept."
                        )
                        build_status.classes(replace="text-xs text-green-400 mt-1")
                    except Exception as ex:
                        build_status.text = f"Error: {ex}"
                        build_status.classes(replace="text-xs text-red-400 mt-1")
                    finally:
                        build_btn.enable()

                build_btn = ui.button(
                    "Build with AI", icon="smart_toy", on_click=_build,
                ).classes("bg-purple-700 text-white mt-3 px-4 py-2 text-sm")

            # ── Card 3: Strategy description ──────────────────────────────────
            desc_card = ui.card().classes("flex-1 bg-gray-800 p-4 rounded-lg min-w-56")

            def _refresh_desc(key: str):
                desc_card.clear()
                with desc_card:
                    text = _get_desc(key)
                    if text:
                        ui.markdown(text).classes("text-sm text-gray-300")
                    else:
                        ui.label("No description available.").classes(
                            "text-xs text-gray-500 italic"
                        )

            _refresh_desc(display_id)
            sel.on_value_change(lambda e: _refresh_desc(e.value))

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
