"""Trade closing -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.close_trade/_record_close/
_close_all_ladder_legs/_get_trading_balance, as part of the core/engine.py
migration series. See docs/todo/refactor/core-close-trade-migration/020-*.md.

close_trade/close_all_ladder_legs call bridge.close_position -- the real
MT5 order-close call, unchanged from the original. This module places no
order itself; it only calls whatever `bridge` its caller supplies.

Reuses core_fees_sizing.pnl() (pack 1), core_risk_governor.
rg_apply_halts_on_close() (pack 1), core_dpm_bookkeeping.
finalize_dpm_record() (pack 4) instead of the original self.pnl()/
self._rg_apply_halts_on_close()/self._finalize_dpm_record().

CloseTradeContext bundles the collaborators these functions need that
aren't derivable from the database: the MT5 bridge, a TP-cache instance
(pack 5's TPCache), and a few small pieces of not-yet-extracted instance
state (scale-out retry cooldowns, TP Safety Net alert throttling,
profit-sound notification) that belong to deferred subsystems but get
touched here for cleanup/notification on close -- taken as plain
externally-owned dicts/callbacks rather than extracting those subsystems.
_schedule_profit_sync/_background_close_commentary are much larger,
separate pieces of work (a 30-minute retry loop; an AI/Telegram
notification) -- taken as optional injected async callables (default:
no-op) so this module's own behavior is fully testable without building
either.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.trading.fees_sizing import pnl as _pnl
from backend.src.services.risk.governor import rg_apply_halts_on_close
from backend.src.services.dpm.bookkeeping import finalize_dpm_record
from backend.src.services.positions.tp_tracking import TPCache

log = logging.getLogger(__name__)


class CloseTradeContext:
    """Collaborators needed by close_trade/record_close/close_all_ladder_legs
    that aren't derivable from the database. Callers own this and pass the
    same instance across calls so cache/dict state persists correctly."""

    def __init__(
        self,
        bridge: Any,
        *,
        starting_balance: float = 1000.0,
        tp_cache: Optional[TPCache] = None,
        scale_out_last_fail: Optional[dict] = None,
        tp_safety_net_last_alert: Optional[dict] = None,
        on_profit: Optional[Callable[[], None]] = None,
        schedule_profit_sync: Optional[Callable[[str, int], Awaitable[None]]] = None,
        background_close_commentary: Optional[Callable[[str, dict, str, Any], Awaitable[None]]] = None,
    ):
        self.bridge = bridge
        self.starting_balance = starting_balance
        self.tp_cache = tp_cache if tp_cache is not None else TPCache()
        self.scale_out_last_fail = scale_out_last_fail if scale_out_last_fail is not None else {}
        self.tp_safety_net_last_alert = (
            tp_safety_net_last_alert if tp_safety_net_last_alert is not None else {}
        )
        self.on_profit = on_profit
        self.schedule_profit_sync = schedule_profit_sync
        self.background_close_commentary = background_close_commentary


async def get_trading_balance(bridge: Any, starting_balance: float) -> float:
    """Current account balance for lot-size calculations.
    Prefers the live MT5 bridge balance; falls back to the local simulation account."""
    try:
        mt5_acc = await bridge.get_account()
        if mt5_acc and float(mt5_acc.get("balance") or 0) > 0:
            return float(mt5_acc["balance"])
    except Exception:
        pass
    acc = trade_repo.get_simulation_account()
    return float(acc.get("balance") or starting_balance)


async def close_trade(trade_id: str, reason: str, ctx: CloseTradeContext) -> dict:
    tick = await ctx.bridge.get_tick()
    row = trade_repo.get_trade(trade_id)
    if not row or row["status"] != "open":
        raise ValueError(f"Trade {trade_id} is not open")

    # Adaptive Runner ladder trades have N separate MT5 tickets —
    # row["mt5_ticket"] is only the anchor leg. Closing just that one
    # here (the code below) would leave every other leg open in MT5
    # while the DB marks the whole trade closed. Close every open leg.
    _legs = await db_module.to_db_thread(db_module.get_ladder_legs, trade_id)
    if any(l["status"] == "open" for l in _legs):
        return await close_all_ladder_legs(trade_id, row, _legs, reason, ctx)

    mt5_ticket = row.get("mt5_ticket")
    if tick:
        close_price = tick.bid if row["direction"].upper() == "BUY" else tick.ask
    else:
        if not mt5_ticket:
            log.warning(
                "close_trade %s: no live tick and no MT5 ticket — using entry price as close price",
                trade_id,
            )
        close_price = float(row["entry_price"])

    if mt5_ticket:
        mt5_res = await ctx.bridge.close_position(int(mt5_ticket))
        if mt5_res.get("error"):
            raise RuntimeError(
                f"MT5 close rejected for ticket {mt5_ticket}: {mt5_res.get('error')}"
            )
        if mt5_res.get("success") is False:
            raise RuntimeError(
                f"MT5 close failed for ticket {mt5_ticket}: {mt5_res}"
            )
        if mt5_res.get("success"):
            close_price = float(mt5_res.get("close_price", close_price))

    result = await record_close(trade_id, float(close_price), reason, ctx)

    if mt5_ticket and ctx.schedule_profit_sync:
        asyncio.create_task(ctx.schedule_profit_sync(trade_id, int(mt5_ticket)))

    if tick and ctx.background_close_commentary:
        asyncio.create_task(ctx.background_close_commentary(trade_id, result, reason, tick))

    return result


async def record_close(trade_id: str, close_price: float, reason: str, ctx: CloseTradeContext) -> dict:
    row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
    direction   = row["direction"]
    remaining   = float(row["remaining_lots"])
    entry_price = float(row["entry_price"])
    gross_pnl   = _pnl(direction, entry_price, close_price, remaining)
    net_pnl     = gross_pnl  # actual fees already in mt5_profit; no double-counting here
    prev_realised = float(row.get("realised_pnl", 0))
    prev_net_pnl  = float(row.get("net_pnl", 0))
    now = time.time()

    await db_module.to_db_thread(
        trade_repo.apply_full_close,
        trade_id, now, round(close_price, 2), reason,
        round(gross_pnl, 4), round(prev_realised + gross_pnl, 4),
        round(prev_net_pnl + net_pnl, 4), net_pnl, row["signal_id"],
    )
    if gross_pnl > 0 and ctx.on_profit:
        ctx.on_profit()

    # Invalidate TP trigger cache for this trade (it's now closed)
    ctx.tp_cache.triggered.pop(trade_id, None)
    for _k in [k for k in ctx.scale_out_last_fail if k[0] == trade_id]:
        ctx.scale_out_last_fail.pop(_k, None)
    ctx.tp_safety_net_last_alert.pop(trade_id, None)

    # Update peak balance watermark (used by total-drawdown circuit breaker).
    # Uses the live MT5 balance, not vantage_simulation_account — that table
    # is this app's own internally-computed running P&L ledger, which can
    # and does drift from the real account over many trades. Peak and
    # current must come from the same (live) source for the comparison to
    # mean anything.
    try:
        live_balance = await get_trading_balance(ctx.bridge, ctx.starting_balance)

        def _update_peak_balance():
            old_peak = float(db_module.get_app_config("peak_balance") or 0)
            if live_balance > old_peak:
                db_module.set_app_config("peak_balance", str(round(live_balance, 4)))
        await db_module.to_db_thread(_update_peak_balance)
    except Exception as _pe:
        log.debug("[RG] peak balance update failed: %s", _pe)
        live_balance = None

    result = {
        "trade_id":    trade_id,
        "close_price": close_price,
        "gross_pnl":   gross_pnl,
        "net_pnl":     net_pnl,
        "reason":      reason,
    }

    try:
        from backend.src.controllers.sync.ledger import push_trade_closed
        _rr = None
        try:
            _entry = row.get("entry_price")
            _sl = row.get("stop_loss")
            _tp1 = row.get("tp1")
            if _entry is not None and _sl is not None and _tp1 is not None:
                _entry, _sl, _tp1 = float(_entry), float(_sl), float(_tp1)
                if _sl != _entry:
                    _rr = abs(_tp1 - _entry) / abs(_entry - _sl)
        except Exception:
            _rr = None
        push_trade_closed({
            "trade_id":    trade_id,
            "engine":      "main",
            "direction":   direction,
            "strategy":    row.get("strategy", ""),
            "open_time":   row.get("open_time"),
            "close_time":  now,
            "pnl_dollars": round(prev_net_pnl + net_pnl, 4),
            "outcome":     "win" if gross_pnl > 0.5 else ("loss" if gross_pnl < -0.5 else "be"),
            "tg_source":   row.get("tg_source"),
            "mt5_ticket":  row.get("mt5_ticket"),
            "rr":          _rr,
        })
    except Exception as _le:
        log.debug("[Ledger] push failed: %s", _le)

    # Persist DPM learning record if DPM was active for this trade
    try:
        rs = await db_module.to_db_thread(db_module.get_risk_settings)
        if bool(rs.get("dpm_enabled", 0)):
            total_pnl = round(prev_net_pnl + net_pnl, 4)
            await db_module.to_db_thread(
                finalize_dpm_record, trade_id, close_price, reason, total_pnl
            )
    except Exception as _e:
        log.debug("[DPM] finalize_dpm_record skipped: %s", _e)

    # Tier 1 Risk Governor: re-evaluate circuit breakers after each close.
    try:
        _rg_rs = await db_module.to_db_thread(db_module.get_risk_settings)
        if bool(_rg_rs.get("risk_governor_enabled", 0)):
            # Reuse the live balance fetched above for the peak watermark —
            # same reasoning: must be the same (live) source as the peak
            # comparison, not vantage_simulation_account.
            _rg_balance = live_balance if live_balance is not None else await get_trading_balance(
                ctx.bridge, ctx.starting_balance
            )
            await db_module.to_db_thread(rg_apply_halts_on_close, _rg_rs, _rg_balance)
    except Exception as _rg_e:
        log.debug("[RG] post-close halt check skipped: %s", _rg_e)

    # Global circuit breaker — only count live MT5 trade outcomes.
    if row.get("mt5_ticket"):
        try:
            total_pnl = round(float(row.get("net_pnl", 0)) + net_pnl, 4)
            new_cb = await db_module.to_db_thread(db_module.record_live_trade_outcome, won=total_pnl >= 0)
            if new_cb.get("just_triggered"):
                cooldown_mins = new_cb.get("cooldown_mins", 60)
                threshold = new_cb.get("losses_threshold", 3)
                log.warning(
                    "[CB] Circuit breaker triggered — live trading blocked for %d min.", cooldown_mins
                )
                from backend.src.services.telegram import alerts as telegram_alerts
                asyncio.create_task(telegram_alerts.send_message(
                    f"*Circuit Breaker Triggered*\n"
                    f"{threshold} consecutive live losses — new trade execution is "
                    f"paused for {cooldown_mins} minutes. Signal generators keep "
                    f"running; no new orders will be placed until the cooldown "
                    f"expires or you reset it manually in Risk Settings.",
                    row.get("trade_id", ""), "circuit_breaker_triggered",
                ))
        except Exception as _cb_e:
            log.debug("[CB] outcome recording skipped: %s", _cb_e)

    return result


async def close_all_ladder_legs(
    trade_id: str, row: dict, legs: list[dict], reason: str, ctx: CloseTradeContext,
) -> dict:
    """Manual/forced full close of an Adaptive Runner ladder trade —
    closes every still-open leg at market (each is its own MT5 ticket),
    sums their real P&L, and records the parent trade closed. Used by
    close_trade() so /close and the UI's Close button don't orphan
    every leg but the anchor."""
    direction     = row["direction"]
    parent_entry  = float(row["entry_price"])
    total_pnl = 0.0
    last_price = float(row.get("close_price") or parent_entry)
    closed_any = False
    for leg in legs:
        if leg["status"] != "open":
            continue
        ticket = leg.get("mt5_ticket")
        if not ticket:
            continue
        leg_entry_price = float(leg.get("entry_price") or parent_entry)
        try:
            mt5_res = await ctx.bridge.close_position(int(ticket))
        except Exception as _e:
            log.warning("[adaptive_runner_ladder] manual close failed leg=%s ticket=%s: %s",
                       leg["tier"], ticket, _e)
            continue
        if not mt5_res.get("success"):
            log.warning("[adaptive_runner_ladder] manual close rejected leg=%s ticket=%s: %s",
                       leg["tier"], ticket, mt5_res.get("error"))
            continue
        close_price = float(mt5_res.get("close_price", leg["tp_price"]))
        leg_pnl = _pnl(direction, leg_entry_price, close_price, float(leg["lots"]))
        await db_module.to_db_thread(
            db_module.close_ladder_leg, leg["id"], close_price, leg_pnl, "closed_manual",
        )
        total_pnl += leg_pnl
        last_price = close_price
        closed_any = True

    await db_module.to_db_thread(
        trade_repo.apply_ladder_close,
        trade_id, closed_any,
        sum(float(l["lots"]) for l in legs if l["status"] == "open"),
        last_price, total_pnl, reason, row["signal_id"], time.time(),
    )

    result = {
        "trade_id":    trade_id,
        "close_price": last_price,
        "gross_pnl":   total_pnl,
        "net_pnl":     round(float(row.get("net_pnl", 0)) + total_pnl, 4),
        "reason":      reason,
    }
    anchor_ticket = row.get("mt5_ticket")
    if anchor_ticket and ctx.schedule_profit_sync:
        asyncio.create_task(ctx.schedule_profit_sync(trade_id, int(anchor_ticket)))
    return result
