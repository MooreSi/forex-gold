"""MT5 deal-history performance stats -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine.compute_mt5_performance
(plus the module-private _apply_fee/_platform_fee_rate helpers it alone
uses), as part of the core/engine.py migration series. See
docs/todo/refactor/core-mt5-history-migration/020-*.md.

Takes `bridge` as an explicit parameter (anything exposing async
get_account()/get_deal_history(days)/get_positions(), matching
MT5BridgeClient's real shape) instead of reading self._bridge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.src.db import database as db_module
from backend.src.utils.trading_clock import local_from, local_timestamp

log = logging.getLogger(__name__)

BROKER_OFFSET = 10800  # the broker stores UTC+3 timestamps as-if-UTC

# "No offset given, resolve it" as distinct from an explicit None, which means
# "this machine's own clock" and is a real answer rather than a missing one.
_UNSET = object()


def broker_day_cutoff(offset_minutes=_UNSET, now: datetime = None) -> float:
    """Local midnight, expressed in the broker-timestamp space deals carry.

    "Today's P&L" has to begin at the user's midnight. It was Europe/London's
    until 2026-09-01, which is right for the owner and wrong for anyone else:
    in Sydney "today" began at 09:00 or 10:00 local and swept in most of the
    previous afternoon.

    Deals carry broker time (real UTC + 3h), so the answer is converted back
    into that space rather than the caller converting each deal out of it.

    Both parameters are for tests; the real call site passes neither.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if offset_minutes is _UNSET:
        from backend.src.services.risk import clock as _clock
        offset_minutes = _clock.offset_minutes()
    local = local_from(now, offset_minutes)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_timestamp(midnight, offset_minutes) + BROKER_OFFSET


def _platform_fee_rate() -> float:
    """Return the combined round-turn commission per standard lot from fee settings.

    In demo mode MT5 never charges commission, so we estimate from the configured
    rate so that P&L figures are realistic. On a live ECN account the actual MT5
    deal `fee` field is used directly and this value is ignored. Shared with
    ui/pages/history.py so every P&L surface (Closed Trades table, equity curve,
    calendar heatmap, this engine's own performance stats) agrees on the same
    net figure instead of some showing gross MT5 profit and others net-of-fees."""
    try:
        fs = db_module.get_fee_settings()
        return (
            float(fs.get("commission_per_lot_per_side", 0.0)) * 2
            + float(fs.get("commission_round_turn_per_lot", 0.0))
        )
    except Exception:
        return 0.0


def _apply_fee(pos_deals: list, open_lots: float, comm_rate: float) -> tuple[float, float]:
    """Return (pnl_after_fees, fees_charged) for one closed MT5 position's deals.

    Uses the actual MT5 `fee` field when the account is charged commission (live
    ECN). Falls back to `open_lots × comm_rate` when the fee field is zero (demo
    or no-commission account), so the figure stays realistic in either mode."""
    raw_pnl = round(sum(
        float(d.get("profit", 0)) + float(d.get("swap", 0)) + float(d.get("fee", 0))
        for d in pos_deals
    ), 2)
    mt5_fee_abs = abs(round(sum(float(d.get("fee", 0)) for d in pos_deals), 2))
    if mt5_fee_abs > 0:
        # Live ECN: fee already deducted inside raw_pnl
        return raw_pnl, mt5_fee_abs
    # Demo / no-commission: estimate and deduct so P&L is realistic
    est_fee = round(open_lots * comm_rate, 2) if (open_lots > 0 and comm_rate > 0) else 0.0
    return round(raw_pnl - est_fee, 2), est_fee


async def compute_mt5_performance(bridge: Any, days: int = 90) -> dict:
    """Compute performance stats directly from MT5 bridge deal history."""
    try:
        account = await bridge.get_account()
        deals   = await bridge.get_deal_history(days) or []

        by_pos: dict[int, list] = {}
        for d in deals:
            pid = d.get("position_id")
            if pid:  # excludes None and 0 (balance/deposit ops)
                by_pos.setdefault(int(pid), []).append(d)

        # "Today" starts at the user's own midnight -- see broker_day_cutoff.
        _broker_day_cutoff = broker_day_cutoff()

        # Collect (close_time, pnl) so we can sort chronologically before drawdown calc
        _trade_rows: list[tuple[float, float]] = []
        daily_pnl = 0.0
        _daily_pnl_list: list[float] = []
        _comm_rate = _platform_fee_rate()

        for ticket, pos_deals in by_pos.items():
            _cd = [d for d in pos_deals if d.get("entry") in (1, 2, 3)]
            close_deal = max(_cd, key=lambda d: d.get("time", 0)) if _cd else None
            if not close_deal:
                continue
            close_ts = float(close_deal.get("time", 0))
            _open_deal = next((d for d in pos_deals if d.get("entry") == 0), None)
            _open_lots = float(_open_deal.get("volume", 0)) if _open_deal else float(close_deal.get("volume", 0))
            # Net of estimated fees (same _apply_fee logic as the Closed Trades
            # table/equity curve/calendar in history.py) so every P&L surface in
            # the app agrees on the same figure rather than this one showing raw
            # MT5 profit (fee-free on a demo account) while others net it out.
            pnl, _fees = _apply_fee(pos_deals, _open_lots, _comm_rate)
            _trade_rows.append((close_ts, pnl))
            if close_ts >= _broker_day_cutoff:
                daily_pnl += pnl
                _daily_pnl_list.append(pnl)

        # Sort by close time — required for a correct equity-curve drawdown calculation
        _trade_rows.sort(key=lambda x: x[0])
        trades_pnl = [p for _, p in _trade_rows]

        balance = float((account or {}).get("balance", 0) or 0)
        equity  = float((account or {}).get("equity",  0) or 0)
        open_positions = await bridge.get_positions() or []
        open_pnl = sum(float(p.get("profit", 0)) for p in open_positions)

        winners  = [p for p in trades_pnl if p > 0]
        losers   = [p for p in trades_pnl if p < 0]
        win_rate = len(winners) / len(trades_pnl) * 100 if trades_pnl else 0.0
        pf = (sum(winners) / abs(sum(losers))
              if losers and sum(winners) > 0 else 0.0)

        # Max drawdown / run-up — walk the chronological equity curve.
        # Reconstruct the starting balance for the analysis period.
        # If balance - sum(trades_pnl) <= 0 (demo reset mid-period, or
        # period history pre-dates the current account state), clamp to
        # the current balance rather than 0 — a zero start makes any
        # win→breakeven pair register as 100% drawdown.
        _raw_start = balance - sum(trades_pnl)
        _period_start = _raw_start if _raw_start > 0 else (balance if balance > 0 else 1.0)
        peak = cum = _period_start
        trough = cum
        max_dd = 0.0
        max_ru = 0.0
        for p in trades_pnl:
            cum += p
            if cum < trough:
                trough = cum
            if cum > peak:
                peak = cum
            # Drawdown: loss from running peak — checked every step (standard)
            if peak > 0:
                dd = (peak - cum) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            # Run-up: gain from running trough — checked every step (mirror of drawdown)
            if trough > 0:
                ru = (cum - trough) / trough * 100
                if ru > max_ru:
                    max_ru = ru

        _daily_wins = [p for p in _daily_pnl_list if p > 0]
        _daily_wr   = (
            round(len(_daily_wins) / len(_daily_pnl_list) * 100, 1)
            if _daily_pnl_list else 0.0
        )
        return {
            "balance":            balance,
            "equity":             equity,
            "open_pnl":           round(open_pnl, 2),
            "closed_trades":      len(trades_pnl),
            "open_trades":        len(open_positions),
            "win_rate_pct":       round(win_rate, 2),
            "profit_factor":      round(pf, 4),
            "total_net_pnl":      round(sum(trades_pnl), 4),
            "best_trade":         round(max(trades_pnl), 4) if trades_pnl else 0.0,
            "worst_trade":        round(min(trades_pnl), 4) if trades_pnl else 0.0,
            "max_drawdown_pct":   round(max_dd, 2),
            "max_runup_pct":      round(max_ru, 2),
            # Return on Investment over the period -- net P&L as a percentage
            # of the reconstructed starting balance (_period_start above).
            "roi_pct":            round(sum(trades_pnl) / _period_start * 100, 2) if _period_start > 0 else 0.0,
            # UK calendar-day stats
            "daily_pnl_24h":      round(daily_pnl, 4),   # kept for email compat
            "daily_pnl":          round(daily_pnl, 4),
            "daily_closed":       len(_daily_pnl_list),
            "daily_win_rate_pct": _daily_wr,
            "daily_best":         round(max(_daily_pnl_list), 2) if _daily_pnl_list else 0.0,
            "daily_worst":        round(min(_daily_pnl_list), 2) if _daily_pnl_list else 0.0,
            "source":             "mt5",
        }
    except Exception as e:
        log.debug("compute_mt5_performance error: %s", e)
        return {}
