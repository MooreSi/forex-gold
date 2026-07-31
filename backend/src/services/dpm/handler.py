"""Dynamic Position Management (DPM) handler + calibration sweep --
extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine._handle_dynamic_position_management/_run_dpm_calibration,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-dpm-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

`dpm_engine.compute_adaptive_params`/`run_calibration` are an already-
extracted, stable, pure module (ATR/ADX/session/momentum computation from
real candle data) -- untouched by this pack, called through exactly as the
original did.

Reuses core_dpm_bookkeeping.py's DPMCache/load_dpm_calibrated/
record_dpm_entry/update_dpm_peak/set_dpm_milestone (already extracted),
core_tp_trigger_tracking.py's TPCache/get_triggered_tps/get_remaining_lots,
core_partial_close.partial_close_trade, and core_fees_sizing.pnl.

Note: partial_close_trade has its own independent "move SL to breakeven on
TP1" logic (see 010's Notes) which fires whenever the TP1 branch actually
calls it (tp1_partial_pct > 0), making this handler's own SL-to-BE move a
redundant-but-harmless duplicate in that case -- except when
tp1_partial_pct == 0, where partial_close_trade is never called and the
handler's own move is the only mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from forex_trader.core import database as db_module
from backend.src.services.dpm import engine as dpm_engine
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.dpm.bookkeeping import (
    DPMCache, load_dpm_calibrated, record_dpm_entry, update_dpm_peak, set_dpm_milestone,
)
from forex_trader.core.core_fees_sizing import pnl
from forex_trader.core.core_partial_close import partial_close_trade
from backend.src.services.positions.tp_tracking import TPCache, get_triggered_tps, get_remaining_lots
from backend.src.utils.models import MAX_TP, Tick

log = logging.getLogger(__name__)


async def run_dpm_calibration(dpm_cache: DPMCache) -> None:
    """
    Calibrate DPM multipliers from completed trade history.
    Triggers when at least 5 new closed DPM trades exist since the last run,
    with a 2-hour minimum gap to prevent back-to-back runs in busy sessions.
    Results are stored in dpm_calibration and loaded into dpm_cache.calibrated.
    """
    last_run_str   = db_module.get_app_config("dpm_cal_last_run")   or "0"
    last_count_str = db_module.get_app_config("dpm_cal_trade_count") or "0"
    try:
        last_run   = float(last_run_str)
        last_count = int(last_count_str)
    except ValueError:
        last_run   = 0.0
        last_count = 0

    # Enforce 2-hour minimum gap between runs
    if time.time() - last_run < 7_200:
        return

    with db_module.db() as conn:
        trades = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM dpm_trade_performance "
                "WHERE closed_at IS NOT NULL AND final_pnl IS NOT NULL"
            ).fetchall()
        ]

    if len(trades) < 20:
        log.debug("[DPM Cal] Only %d closed trades — need 20+ to calibrate", len(trades))
        return

    # Require at least 5 new closed trades since the last calibration run
    new_since_last = len(trades) - last_count
    if new_since_last < 5:
        log.debug("[DPM Cal] Only %d new trades since last run — need 5", new_since_last)
        return

    log.info("[DPM Cal] Running calibration on %d closed DPM trades (%d new)",
             len(trades), new_since_last)
    results = dpm_engine.run_calibration(trades)

    if not results:
        log.debug("[DPM Cal] No groups with enough samples")
        return

    with db_module.db() as conn:
        for r in results:
            conn.execute(
                """INSERT INTO dpm_calibration
                   (calibrated_at, session, momentum_bucket, be_multiplier, trail_multiplier,
                    tp1_partial_pct, sample_size, profit_factor, win_rate, avg_r_multiple, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["calibrated_at"], r["session"], r["momentum_bucket"],
                 r["be_multiplier"], r["trail_multiplier"], r["tp1_partial_pct"],
                 r["sample_size"], r["profit_factor"], r["win_rate"],
                 r["avg_r_multiple"], r["notes"]),
            )

    db_module.set_app_config("dpm_cal_last_run", str(time.time()))
    db_module.set_app_config("dpm_cal_trade_count", str(len(trades)))
    # Force reload on next cycle
    dpm_cache.loaded_at = 0.0
    log.info("[DPM Cal] Calibration complete — %d groups updated", len(results))


async def handle_dynamic_position_management(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    dpm_cache: DPMCache,
    dpm_candles: list,
    dpm_dxy_candles: Optional[list],
) -> None:
    """
    Adaptive DPM — all parameters computed from ATR, session, momentum and
    structural swing levels.  No manual tuning required.

    Priority order each tick:
      1. If TP1 cleared: partial close (momentum-scaled %) → SL to entry.
      2. If unrealised P&L >= adaptive BE trigger: SL to entry.
      3. If SL already at entry: adaptive trailing SL (arithmetic or structural,
         whichever locks in more profit).
      4. Mark TP2+ chips for UI (no close — trailing handles the exit).
    """
    calibrated  = await db_module.to_db_thread(load_dpm_calibrated, dpm_cache)
    triggered   = await get_triggered_tps(tp_cache, trade["trade_id"])
    post_tp1    = 1 in triggered
    trade_id    = trade["trade_id"]

    # Read sl_moved_to_be from DB to avoid one-cycle lag — needed before
    # compute_adaptive_params below so the post-BE tightened trail (see
    # that flag's use there) also applies to trades that locked breakeven
    # via the direct BE-trigger threshold (step 2 below) rather than TP1.
    # Previously this only tightened after TP1 specifically, so a trade
    # that reached BE without ever touching TP1 kept the full pre-BE
    # trail width the whole way to its eventual stop — the single
    # biggest source of giving back locked-in profit (2026-07-13 GD
    # Copy sample: 7/7 such trades averaged giving back ~76% of peak
    # unrealised P&L, vs ~28% for trades that tightened via TP1).
    sl_at_be = bool(trade.get("sl_moved_to_be"))
    if not sl_at_be:
        def _fetch_be_flag():
            with db_module.db() as conn:
                return conn.execute(
                    "SELECT sl_moved_to_be FROM vantage_simulated_trades WHERE trade_id=?",
                    (trade_id,),
                ).fetchone()
        row = await db_module.to_db_thread(_fetch_be_flag)
        if row and row[0]:
            sl_at_be = True

    params      = dpm_engine.compute_adaptive_params(
        trade, tick, dpm_candles,
        calibrated=calibrated,
        dxy_candles=dpm_dxy_candles,
        post_tp1=(post_tp1 or sl_at_be),
    )
    trail_dist  = params["trail_distance"]
    be_trigger  = params["be_trigger_usd"]
    tp1_pct     = params["tp1_partial_pct"]
    swing_sl    = params["swing_sl"]

    # Persist reasoning for the UI status row
    await db_module.to_db_thread(
        db_module.set_app_config, f"dpm_status_{trade['trade_id']}", params["reasoning"]
    )

    # Record entry snapshot on first cycle; update peak PnL every cycle
    await db_module.to_db_thread(record_dpm_entry, dpm_cache, trade, params)

    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"])
    mt5_ticket  = trade.get("mt5_ticket")
    lot_size    = float(trade["lot_size"])

    current    = tick.bid if direction == "BUY" else tick.ask
    unrealized = pnl(direction, entry_price, current, float(trade["remaining_lots"]))

    # Update high-water P&L for calibration
    await db_module.to_db_thread(update_dpm_peak, trade_id, unrealized)

    # ── 1. TP1: partial close + SL to entry ──────────────────────────────
    tp1_val = trade.get("tp1")
    if 1 not in triggered and tp1_val is not None:
        tp1_vf = float(tp1_val)
        tp1_cleared = (
            (direction == "BUY"  and tp1_vf > entry_price and tick.bid >= tp1_vf) or
            (direction == "SELL" and tp1_vf < entry_price and tick.ask <= tp1_vf)
        )
        if tp1_cleared:
            if tp1_pct > 0:
                remaining     = await db_module.to_db_thread(get_remaining_lots, trade_id)
                lots_to_close = min(round(lot_size * tp1_pct, 4), remaining)
                if lots_to_close > 0:
                    actual_price = tp1_vf
                    if mt5_ticket:
                        mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
                        if mt5_res.get("success"):
                            actual_price = float(mt5_res.get("close_price", tp1_vf))
                            lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
                        elif mt5_res.get("error") or mt5_res.get("success") is False:
                            log.warning("[DPM] MT5 TP1 partial close failed ticket=%s: %s",
                                        mt5_ticket, mt5_res)
                            return
                    try:
                        res = await partial_close_trade(trade_id, lots_to_close, actual_price, "TP1")
                        asyncio.create_task(telegram_alerts.send_message(
                            telegram_alerts.fmt_tp_hit(trade, 1, actual_price, lots_to_close,
                                                       res.get("partial_pnl", 0)),
                            trade_id, "tp1_hit",
                        ))
                        log.info("[DPM] %s TP1 partial close %.2f lots @ %.2f (%.0f%% | %s)",
                                 trade_id[:8], lots_to_close, actual_price,
                                 tp1_pct * 100, params["momentum_label"])
                    except Exception as exc:
                        log.warning("[DPM] TP1 partial close failed: %s", exc)

            await db_module.to_db_thread(set_dpm_milestone, trade_id, "reached_tp1")
            if not sl_at_be:
                should_move = (direction == "BUY" and entry_price > current_sl) or \
                              (direction == "SELL" and entry_price < current_sl)
                if should_move:
                    if mt5_ticket:
                        await bridge.modify_order(int(mt5_ticket), sl=entry_price, tp=None)
                    def _apply_tp1_be():
                        with db_module.db() as conn:
                            conn.execute(
                                "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1"
                                " WHERE trade_id=?",
                                (entry_price, trade_id),
                            )
                    await db_module.to_db_thread(_apply_tp1_be)
                    asyncio.create_task(telegram_alerts.send_message(
                        telegram_alerts.fmt_sl_moved(trade, 1, entry_price),
                        trade_id, "sl_moved_be",
                    ))
            return

    # ── 2. BE move: lock entry when P&L crosses adaptive threshold ────────
    if not sl_at_be and unrealized >= be_trigger:
        should_move = (direction == "BUY" and entry_price > current_sl) or \
                      (direction == "SELL" and entry_price < current_sl)
        if should_move:
            if mt5_ticket:
                await bridge.modify_order(int(mt5_ticket), sl=entry_price, tp=None)
            def _apply_be_trigger():
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1"
                        " WHERE trade_id=?",
                        (entry_price, trade_id),
                    )
            await db_module.to_db_thread(_apply_be_trigger)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_sl_moved(trade, 1, entry_price),
                trade_id, "sl_moved_be_dpm",
            ))
            await db_module.to_db_thread(set_dpm_milestone, trade_id, "reached_be")
            log.info("[DPM] %s BE triggered @ $%.2f profit (threshold $%.2f | ATR $%.1f)",
                     trade_id[:8], unrealized, be_trigger, params["atr"])
        return

    # ── 3. Adaptive trailing (once BE is locked) ─────────────────────────
    if sl_at_be:
        # If the trade hasn't moved far enough for the full trail to clear entry,
        # tighten the trail to 60% of the current price move so profit can still
        # be locked.  This prevents the SL from sitting at BE the whole trade when
        # trail_dist > price_move_from_entry.
        price_move = abs(current - entry_price)
        effective_trail = trail_dist
        if price_move > 0 and trail_dist > price_move:
            effective_trail = round(price_move * 0.60, 2)

        if direction == "BUY":
            arith_sl = round(tick.bid - effective_trail, 2)
            # Use structural level if it locks in more profit than arithmetic trail
            new_sl   = max(arith_sl, swing_sl or arith_sl, entry_price)
            should_update = new_sl > current_sl
        else:
            arith_sl = round(tick.ask + effective_trail, 2)
            new_sl   = min(arith_sl, swing_sl or arith_sl, entry_price)
            should_update = new_sl < current_sl

        if should_update:
            if mt5_ticket:
                await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
            def _apply_dpm_trail():
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                        (new_sl, trade_id),
                    )
            await db_module.to_db_thread(_apply_dpm_trail)
            src = "structural" if swing_sl and (
                (direction == "BUY"  and new_sl == swing_sl) or
                (direction == "SELL" and new_sl == swing_sl)
            ) else "arithmetic"
            log.debug("[DPM] %s trail SL → %.2f (%s | trail $%.1f)",
                      trade_id[:8], new_sl, src, trail_dist)

    # ── 4. Mark TP2+ chips for UI (no close — trailing handles exit) ─────
    for tp_num in range(2, MAX_TP + 1):
        if tp_num in triggered:
            continue
        tp_val = trade.get(f"tp{tp_num}")
        if tp_val is None:
            continue
        tp_vf2 = float(tp_val)
        if direction == "BUY"  and tp_vf2 <= entry_price:
            continue
        if direction == "SELL" and tp_vf2 >= entry_price:
            continue
        tp_hit = (direction == "BUY" and tick.bid >= tp_vf2) or \
                 (direction == "SELL" and tick.ask <= tp_vf2)
        if tp_hit:
            def _mark_dpm_tp(tp_num=tp_num, tp_vf2=tp_vf2):
                with db_module.db() as conn:
                    conn.execute(
                        "INSERT INTO vantage_partial_closes"
                        " (trade_id,ts,lots_closed,close_price,pnl,reason)"
                        " VALUES (?,?,?,?,?,?)",
                        (trade_id, time.time(), 0.0, tp_vf2, 0.0, f"TP{tp_num}_DPM_MARKER"),
                    )
            await db_module.to_db_thread(_mark_dpm_tp)
