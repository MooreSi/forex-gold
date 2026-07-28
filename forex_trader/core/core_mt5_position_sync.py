"""MT5 closed-position reconciliation -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._sync_closed_mt5_positions,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-mt5-position-sync-migration/020-*.md.

Never places, closes, or modifies a live MT5 order itself -- it only reads
bridge state and writes to the DB via already-extracted, already-
characterized helpers (partial_close_trade, record_close, sync_profit,
schedule_profit_sync).

`missing_streak` (per-trade consecutive-miss counter) is taken as an
explicit parameter -- instance state that isn't derivable from the
database, same pattern as `retry_after` in the pending-signal-activation
pack.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_close_trade import CloseTradeContext, record_close
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_profit_sync import schedule_profit_sync, sync_profit
from forex_trader.core.core_tp_trigger_tracking import last_closed_tp
from forex_trader.core.models import STRATEGY_SCALE_OUT

log = logging.getLogger(__name__)

MT5_SYNC_MISS_THRESHOLD = 2  # consecutive cycles before treating a missing ticket as a real close


async def sync_closed_mt5_positions(
    bridge: Any, missing_streak: dict, starting_balance: float = 1000.0,
) -> None:
    if not bridge.is_configured():
        return
    def _fetch_open_trades():
        with db_module.db() as conn:
            # Excludes managed_by='ea' trades: the native EA already pushes
            # its own trade_closed event the moment it detects the position
            # gone (ea_bridge.py's _on_trade_closed), which calls the same
            # _record_close() + sends the same Telegram alert this function
            # would otherwise send a second time. _record_close() has no
            # idempotency guard, so without this exclusion both paths race
            # to close the trade and the user gets a duplicate "Stop Loss
            # Hit" message (confirmed live, ticket 1572181515, 2026-07-10 —
            # single DB row, single node, no dual-node involvement at all).
            # Non-EA-managed trades still need this poll as their only
            # detection of an out-of-band MT5-side close.
            # Also excludes any trade with vantage_ladder_legs rows (Adaptive
            # Runner ladder trades): this trade's own mt5_ticket is only
            # leg 1/the anchor, so once leg 1 closes at its own native TP
            # this loop would see the anchor ticket vanish, compute
            # closed_volume from JUST that leg, and — since
            # _handle_adaptive_runner_ladder had already subtracted leg 1's
            # lots from remaining_lots — call partial_close_trade() a
            # SECOND time for the same lots every monitor cycle (the
            # ticket never reappears, so the miss-streak keeps re-firing),
            # draining remaining_lots to 0 and marking the whole parent
            # trade closed within seconds even though legs 2-N are still
            # genuinely open in MT5. Once the parent shows status!='open',
            # _handle_adaptive_runner_ladder (which owns real per-leg
            # closure detection AND survivor SL-trailing) stops being
            # invoked at all for it, orphaning the remaining legs from
            # all further management. Confirmed live 2026-07-17: trades
            # b7dcacbe/bed873ca both closed within ~30s of leg 1, legs
            # 2-N left untracked until a SEPARATE bug (the untracked-
            # position importer not checking vantage_ladder_legs; also
            # fixed below) re-discovered them as phantom duplicate trades.
            return [db_module.row_to_dict(r) for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades WHERE status='open' AND mt5_ticket IS NOT NULL "
                "AND (managed_by IS NULL OR managed_by != 'ea') "
                "AND trade_id NOT IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs)"
            ).fetchall()]
    open_trades = await db_module.to_db_thread(_fetch_open_trades)
    if not open_trades:
        return
    live_positions = await bridge.get_positions()

    # get_positions() returns [] both when the bridge is offline and when
    # there are genuinely no open positions.  Before treating an empty list
    # as "all positions closed", confirm the bridge is actually connected.
    # If MT5 is unreachable (terminal closed, maintenance, bridge restart)
    # an empty list is ambiguous — skip the sync to avoid falsely closing
    # live trades and misfiring Telegram alerts.
    if not live_positions:
        health = await bridge.get_health()
        if not health.get("connected", False):
            log.debug("MT5 sync: skipping — bridge not connected (live_positions empty)")
            return

    live_tickets = {int(p["ticket"]) for p in live_positions}
    deals_by_pos: dict[int, list] = {}
    all_deals = await bridge.get_deal_history(7)
    for d in all_deals:
        pid = d.get("position_id")
        if pid:  # excludes None and 0
            deals_by_pos.setdefault(int(pid), []).append(d)
    tick = await bridge.get_tick()
    for trade in open_trades:
        ticket = int(trade["mt5_ticket"])
        if ticket in live_tickets:
            missing_streak.pop(trade["trade_id"], None)
            continue

        # A ticket can be transiently absent from get_positions() (bridge
        # lock contention, a momentary IPC hiccup) without the position
        # actually having closed. Require MT5_SYNC_MISS_THRESHOLD
        # consecutive misses before acting, so one bad read can't
        # falsely mark a genuinely-open trade as closed.
        streak = missing_streak.get(trade["trade_id"], 0) + 1
        missing_streak[trade["trade_id"]] = streak
        if streak < MT5_SYNC_MISS_THRESHOLD:
            log.warning(
                "MT5 sync: ticket=%s missing from live positions (%d/%d) — "
                "not yet treating as closed",
                ticket, streak, MT5_SYNC_MISS_THRESHOLD,
            )
            continue

        deals = await bridge.get_position_history(ticket)
        if not deals:
            deals = deals_by_pos.get(ticket, [])
        close_price = None
        reason = "MT5_close"
        close_deals: list = []
        if deals:
            # entry 1=OUT, 2=INOUT, 3=OUT_BY (close-by-opposite on hedge accounts)
            close_deals = [d for d in deals if d.get("entry") in (1, 2, 3)]
            if not close_deals:
                open_type = 0 if trade["direction"].upper() == "BUY" else 1
                close_deals = [d for d in deals if d.get("type") != open_type]
            if close_deals:
                best = max(close_deals, key=lambda d: d.get("time", 0))
                close_price = best.get("price")
                comment = (best.get("comment") or "").lower()
                if "sl" in comment or "stop" in comment:
                    reason = "SL"
                elif "tp" in comment or "take" in comment:
                    reason = "MT5_sync_TP"
        if close_price is None:
            close_price = (tick.bid if trade["direction"].upper() == "BUY" else tick.ask) if tick \
                else float(trade.get("entry_price") or 0)
        try:
            # ── Partial-close detection ───────────────────────────────────────
            # If MT5 closed fewer lots than we are tracking, record a partial
            # close and keep the trade open rather than falsely marking it done.
            if close_deals:
                closed_volume = round(
                    sum(float(d.get("volume", 0)) for d in close_deals), 4
                )
                remaining_lots = round(float(trade["remaining_lots"]), 4)
                if closed_volume < remaining_lots - 0.001:
                    partial_profit = round(sum(
                        float(d.get("profit", 0)) + float(d.get("swap", 0))
                        + float(d.get("fee", 0))
                        for d in close_deals
                    ), 2)
                    log.info(
                        "MT5 sync: partial close trade=%s ticket=%s "
                        "closed=%.4f remaining=%.4f profit=%.2f",
                        trade["trade_id"], ticket, closed_volume,
                        remaining_lots - closed_volume, partial_profit,
                    )
                    # `reason` is already "MT5_close"/"MT5_sync_TP" in two of
                    # its three possible values (only "SL" isn't) -- blindly
                    # prefixing produced "MT5_MT5_close"/"MT5_MT5_sync_TP".
                    await partial_close_trade(
                        trade["trade_id"], closed_volume, float(close_price),
                        reason if reason.startswith("MT5_") else f"MT5_{reason}",
                    )
                    # Update ticket if the continuing position has a new ticket
                    new_remaining = round(remaining_lots - closed_volume, 4)
                    for lp in live_positions:
                        lp_vol = round(float(lp.get("volume", 0)), 4)
                        lp_ticket = int(lp.get("ticket", 0))
                        if abs(lp_vol - new_remaining) < 0.001 and lp_ticket != ticket:
                            def _reassign_ticket(lp_ticket=lp_ticket):
                                with db_module.db() as conn:
                                    conn.execute(
                                        "UPDATE vantage_simulated_trades "
                                        "SET mt5_ticket=? WHERE trade_id=?",
                                        (lp_ticket, trade["trade_id"]),
                                    )
                            await db_module.to_db_thread(_reassign_ticket)
                            log.info("MT5 sync: ticket %s → %s (partial close continues)",
                                     ticket, lp_ticket)
                            break
                    asyncio.create_task(telegram_alerts.send_message(
                        telegram_alerts.fmt_mt5_partial_close(
                            trade, closed_volume, float(close_price),
                            new_remaining, partial_profit, reason,
                        ),
                        trade["trade_id"], f"mt5_partial_{reason.lower()}",
                    ))
                    missing_streak.pop(trade["trade_id"], None)
                    continue  # trade still open — do not record as full close

            # ── Full close ────────────────────────────────────────────────────
            missing_streak.pop(trade["trade_id"], None)
            log.info("MT5 sync: closing trade %s ticket=%s @ %.2f reason=%s",
                     trade["trade_id"], ticket, close_price, reason)
            ctx = CloseTradeContext(bridge, starting_balance=starting_balance)
            result = await record_close(trade["trade_id"], float(close_price), reason, ctx)
            await sync_profit(trade["trade_id"], ticket, bridge)
            asyncio.create_task(schedule_profit_sync(trade["trade_id"], ticket, bridge))
            def _fetch_closed_row():
                with db_module.db() as conn:
                    return db_module.row_to_dict(conn.execute(
                        "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
                        (trade["trade_id"],),
                    ).fetchone())
            closed_row = await db_module.to_db_thread(_fetch_closed_row)
            account  = await bridge.get_account()
            last_tp  = await db_module.to_db_thread(last_closed_tp, trade["trade_id"]) if reason == "SL" else None
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(closed_row, result, {}, account,
                                                last_tp=last_tp),
                trade["trade_id"], f"mt5_sync_{reason}",
            ))
        except Exception as e:
            log.warning("MT5 sync close failed %s: %s", trade["trade_id"], e)

    # ── Import any MT5 positions the app doesn't know about ───────────────
    # Covers trades opened directly in MT5 and positions where a partial
    # close on a hedge account replaced the ticket with a new one.
    def _fetch_known_tickets():
        with db_module.db() as _conn:
            known = {
                int(r[0])
                for r in _conn.execute(
                    "SELECT mt5_ticket FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
                ).fetchall()
            }
            # Adaptive Runner ladder legs 2+ are opened via self._bridge.
            # place_order() directly (see _open_adaptive_runner_ladder) —
            # they never get their own vantage_simulated_trades row, only
            # a vantage_ladder_legs row linked to the parent trade. Without
            # this, every non-anchor leg looked "untracked" here and got
            # re-imported as its own phantom duplicate trade (tg_source=
            # MT5_imported) each cycle, inflating open-trade counts against
            # max_open_trades and showing duplicate rows in the UI.
            known |= {
                int(r[0])
                for r in _conn.execute(
                    "SELECT mt5_ticket FROM vantage_ladder_legs WHERE mt5_ticket IS NOT NULL"
                ).fetchall()
            }
            return known
    try:
        all_known_tickets = await db_module.to_db_thread(_fetch_known_tickets)
    except Exception:
        all_known_tickets = set()

    rs = await db_module.to_db_thread(db_module.get_risk_settings)
    default_strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT) or STRATEGY_SCALE_OUT

    for pos in live_positions:
        ticket = int(pos["ticket"])
        if ticket in all_known_tickets:
            continue
        # New untracked position — import it so the engine can manage it
        trade_id = str(uuid.uuid4())[:16]
        direction = pos.get("type", "BUY").upper()
        lot_size  = float(pos.get("volume", 0.01))
        entry_p   = float(pos.get("open_price", 0))
        sl        = float(pos.get("sl") or 0) or None
        tp        = float(pos.get("tp") or 0) or None
        open_ts   = float(pos.get("open_time") or time.time())
        try:
            def _import_position():
                with db_module.db() as conn:
                    # vantage_simulated_trades.signal_id is NOT NULL with a
                    # FOREIGN KEY into vantage_signals — passing "" here (as
                    # this always has, for any directly-MT5-opened position)
                    # can never satisfy that constraint since no row with
                    # signal_id="" has ever existed, so this insert has never
                    # actually succeeded; it just silently failed every
                    # monitor cycle, forever, for any such position. Ensure
                    # a sentinel row exists first (idempotent) instead.
                    conn.execute(
                        """INSERT OR IGNORE INTO vantage_signals
                           (signal_id, source_name, direction, entry_low, entry_high,
                            stop_loss, status, created_at)
                           VALUES ('MT5_DIRECT', 'MT5 direct import', ?, 0, 0, 0,
                                   'activated', ?)""",
                        (direction, time.time()),
                    )
                    conn.execute(
                        """INSERT INTO vantage_simulated_trades
                           (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,
                            entry_price,lot_size,remaining_lots,stop_loss,tp1,
                            status,open_time,strategy,tg_source)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            trade_id, "MT5_DIRECT", ticket, direction,
                            entry_p, entry_p, entry_p,
                            lot_size, lot_size,
                            sl, tp,
                            "open", open_ts, default_strategy, "MT5_imported",
                        ),
                    )
            await db_module.to_db_thread(_import_position)
            log.info(
                "MT5 sync: imported untracked position ticket=%s %s %.2f lots @ %.2f",
                ticket, direction, lot_size, entry_p,
            )
            all_known_tickets.add(ticket)
        except Exception as imp_err:
            log.warning("MT5 sync: failed to import ticket %s: %s", ticket, imp_err)
