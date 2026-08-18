"""Tier 1 Risk Governor: pre-trade filters, position sizing, and halt
logic -- extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine.is_trading_paused/_price_in_entry_range/
_check_pre_trade_filters/_rg_day_start_ts/_rg_size_and_check/
_rg_check_halt/_rg_apply_halts_on_close, as part of the core/engine.py
migration series. See
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.

Deterministic, app-wide safety layer. When risk_governor_enabled is set it
  (A) sizes every position from risk %, (B) enforces a hard per-trade $ ceiling,
  (C) halts on the daily-loss limit, (D) cools down after a loss streak, and
  (E) rejects trades below a minimum TP1 R:R or beyond the directional cap.

The one real bug found in 010: _rg_apply_halts_on_close made two SEPARATE
top-level set_app_config() calls (trade_pause_until, then risk_halt_reason)
-- a crash between them could leave a pause flag with no reason. Fixed
here by wrapping the whole body in one outer `with db_module.db():` --
db_module.db() is thread-local and re-entrant, so the two inner
set_app_config() calls automatically participate in one transaction. No
new adapter/repo needed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from forex_trader.core import database as db_module
from forex_trader.core.models import (
    CONTRACT_SIZE, Tick,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
)

log = logging.getLogger(__name__)

# Channels whose TP/SL levels come directly from a paid signal provider and
# should be taken as-is without the R:R filter. Matching is case-insensitive
# and substring-based so it covers "Telegram Auto (Gold Diggers VIP)" etc.
RR_BYPASS_SOURCES: frozenset = frozenset({
    "gold diggers vip",
    "gold diggers 2.0",
})


def rr_filter_bypassed(source_name: str) -> bool:
    """True when `source_name`'s TP/SL should be taken as-is, unscored.

    Two ways to qualify:

      * the channel is in RR_BYPASS_SOURCES above, or
      * Immediate Market Entry is live for it (the global risk-settings
        toggle AND the channel's own instant_entry_enabled flag, defaulting
        by parser format exactly as core_scan_messages_auto_execute.
        ime_enabled_for_channel and engine.py's own inline gate do).

    The IME arm was added to the scan/auto-execute path on 2026-08-06 by
    explicit user directive -- IME means the user has opted into taking this
    channel's fill at market the moment the signal lands, which an R:R gate
    measured against the live price contradicts by construction. It belongs
    here rather than only at that one call site: the pending-activation
    watcher (core_pending_signal_activation) and resolve_open_trade_params
    (core_signal_resolution) run the same filter over the same signals, so a
    channel exempted on one path was still declined on the others.

    Found live 2026-08-06: every GOLD DIGGERS INSTITUTIONAL signal since
    08-05 12:47 was rejected here (TP1 4-6pt against an 8-9pt stop, 0.47-0.67
    :1 against the 0.75:1 floor) even though the channel has IME on, so the
    app opened no GDI trade at all in that window while its sibling channel
    Gold Diggers VIP -- which only differs by being in the static set above
    -- kept trading.
    """
    _src_lower = (source_name or "").lower()
    if any(ch in _src_lower for ch in RR_BYPASS_SOURCES):
        return True
    if not source_name:
        return False
    try:
        rs = db_module.get_risk_settings() or {}
        if not bool(rs.get("immediate_market_entry", 0)):
            return False
        ch_cfg = db_module.get_channel_parser_config(source_name) or {}
        if not ch_cfg:
            return False
        _default = 1 if ch_cfg.get("parser_format") in ("format_ab", "gd2") else 0
        return bool(ch_cfg.get("instant_entry_enabled", _default))
    except Exception:
        return False


# 0.30 only rejects badly-inverted setups (TP1 closer than 1/3 of the stop).
# A 1:1 floor was tested but blocks the breakeven-mechanism winners that the
# scale-out/conservative strategies rely on, so sizing does the heavy lifting.
RG_MIN_TP1_RR   = 1.00  # minimum TP1 reward:risk required to open (1:1 floor)
RG_MAX_STOP_ATR = 1.5   # reject scalps whose stop is wider than this x ATR


def is_trading_paused() -> bool:
    raw = db_module.get_app_config("trade_pause_until")
    if not raw:
        return False
    try:
        return float(raw) > time.time()
    except Exception:
        return False


def price_in_entry_range(direction: str, entry_low: float, entry_high: float,
                          tick: "Tick") -> bool:
    """Return True if the current price is within (or better than) the entry zone.

    BUY zones are pullback areas: ask must be <= entry_high. Entering above
    entry_high means chasing price past the intended zone.

    SELL zones are rally areas: bid must be >= entry_low. Entering below
    entry_low means chasing price past the zone.

    Filling on the better side of the zone (ask < entry_low for BUY, or
    bid > entry_high for SELL) is allowed -- it is an equal or better fill.
    """
    if direction.upper() == "BUY":
        return tick.ask <= entry_high
    else:
        return tick.bid >= entry_low


def check_pre_trade_filters(
    direction: str,
    entry_low: float,
    entry_high: float,
    stop_loss: float,
    tp1,
    actual_price: Optional[float] = None,
    source_name: str = "",
) -> Optional[str]:
    """
    Evaluate two structural risk filters before opening any trade.

    Filter 1 -- Minimum R:R on TP1 (0.75 : 1)
        Compares TP1 distance against SL distance from the reference price.
        Skipped for channels that supply their own TP/SL levels from a
        signal provider service, or that are running Immediate Market Entry
        -- see rr_filter_bypassed.

    Filter 2 -- Directional cap (max 2 unprotected same-direction trades)
        Blocks a new trade when 2 or more currently-open trades in the same
        direction have not yet reached breakeven (sl_moved_to_be = 0).

    Returns an error string when a filter fires, None when the trade may proceed.
    """
    direction = direction.upper()

    # ── Filter 1: Minimum TP1 R:R ─────────────────────────────────────────
    _MIN_RR = 0.75
    if tp1 is not None and not rr_filter_bypassed(source_name):
        ref_price = float(actual_price) if actual_price is not None \
                    else (entry_low + entry_high) / 2.0
        sl_dist   = abs(ref_price - float(stop_loss))
        tp1_dist  = abs(float(tp1) - ref_price)
        if sl_dist > 0 and (tp1_dist / sl_dist) < _MIN_RR:
            rr = tp1_dist / sl_dist
            price_src = "live price" if actual_price is not None else "zone mid"
            return (
                f"R:R filter blocked: TP1 is {tp1_dist:.1f} pts from {price_src} vs "
                f"SL {sl_dist:.1f} pts away ({rr:.2f}:1 — minimum {_MIN_RR:.2f}:1 required)"
            )

    # ── Filter 2: Directional cap ──────────────────────────────────────────
    _MAX_UNPROTECTED = 2
    with db_module.db() as conn:
        same_dir_unprotected = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades "
            "WHERE status='open' AND direction=? AND sl_moved_to_be=0",
            (direction,),
        ).fetchone()[0]
    if same_dir_unprotected >= _MAX_UNPROTECTED:
        return (
            f"Directional cap blocked: {same_dir_unprotected} open {direction} "
            f"trade(s) are still exposed (SL not at breakeven) — "
            f"max {_MAX_UNPROTECTED} unprotected same-direction trades allowed"
        )

    return None


def rg_day_start_ts() -> float:
    """Epoch of the current trading day's start, in broker time (UTC+3)."""
    broker_now      = datetime.now(timezone.utc) + timedelta(hours=3)
    broker_midnight = broker_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (broker_midnight - timedelta(hours=3)).timestamp()


def day_pnl_and_peak(day_start: Optional[float] = None) -> tuple[float, float]:
    """(realised P&L so far today, the highest it reached today).

    Recomputed from the day's closes every call rather than kept as a stored
    watermark. A stored peak needs resetting at the broker-day boundary and
    survives restarts only if it is persisted correctly -- two things to get
    wrong, both of which fail toward "the guard silently stopped guarding".
    Replaying a day's closed rows is cheap and cannot drift.
    """
    if day_start is None:
        day_start = rg_day_start_ts()
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT net_pnl FROM vantage_simulated_trades "
            "WHERE close_time >= ? ORDER BY close_time", (day_start,),
        ).fetchall()
    running = peak = 0.0
    for (pnl,) in rows:
        running += float(pnl or 0.0)
        peak = max(peak, running)
    return running, peak


def rearm_giveback_guard() -> None:
    """Start the give-back guard's window again from now.

    Called whenever trading is resumed by hand. Without it, Resume is useless
    after a guard halt: the day's peak has already been given back, so the
    guard re-trips on the very next close and stops the day again. Re-arming
    means it only fires again if the day makes a NEW peak past the arming
    amount from here and then hands that back -- which is what someone
    resuming is asking for, rather than an override that switches the
    protection off for the rest of the day.
    """
    db_module.set_app_config("giveback_baseline_ts", str(time.time()))


def _giveback_window_start() -> float:
    """Where the guard starts measuring: the broker day, or the last manual
    resume if that came later."""
    day_start = rg_day_start_ts()
    try:
        baseline = float(db_module.get_app_config("giveback_baseline_ts") or 0)
    except (TypeError, ValueError):
        baseline = 0.0
    return max(day_start, baseline)


def check_giveback_guard(rs: dict) -> Optional[str]:
    """Reason to stop for the day when today's profit has been given back.

    The daily-loss limit measures from the day's OPENING balance, which cannot
    see a day that rises and then bleeds: on 2026-08-17 realised P&L peaked at
    +$348.76 (09:06) and finished -$88.48, and a from-open threshold was never
    breached on the way down. Measuring from the day's PEAK is what makes
    "protect the profit I had" expressible at all.

    Arms only above giveback_arm_usd so ordinary churn near break-even cannot
    lock the day out over a few dollars, and the peak must be positive -- a
    losing day is the daily-loss limit's job, not this one's.
    """
    if not bool(rs.get("giveback_guard_enabled", 0)):
        return None
    arm = float(rs.get("giveback_arm_usd", 50.0) or 0)
    pct = float(rs.get("giveback_pct", 40.0) or 0)
    if arm <= 0 or pct <= 0:
        return None

    realised, peak = day_pnl_and_peak(_giveback_window_start())
    if peak < arm:
        return None
    floor_ = peak * (1.0 - pct / 100.0)
    if realised > floor_:
        return None
    return (
        f"Give-back guard: today peaked at +${peak:.2f} and is now "
        f"${realised:+.2f} — more than {pct:.0f}% of the day's profit handed "
        f"back (floor ${floor_:+.2f})"
    )


def apply_giveback_guard_on_close(rs: dict) -> None:
    """Halt for the rest of the broker day if the give-back guard has tripped.

    Deliberately NOT behind risk_governor_enabled. The governor also takes
    over position sizing and adds its own pre-trade gates, so requiring it
    would mean nobody can have "stop when I give back today's profit" without
    also accepting a different sizing model -- and the account this was written
    for has the governor off, which is precisely why every configured limit on
    it was inert while two days bled out.

    Writes the same trade_pause_until / risk_halt_reason pair the governor
    uses, so /resume, the panel's Resume button and the status line all keep
    working with no special case.
    """
    reason = check_giveback_guard(rs)
    if not reason:
        return
    if is_trading_paused():
        return
    until = rg_day_start_ts() + 86400.0
    with db_module.db():
        db_module.set_app_config("trade_pause_until", str(until))
        db_module.set_app_config("risk_halt_reason", reason)
    log.warning("[RG] %s — trading stopped until the next broker day", reason)


def rg_size_and_check(*, direction: str, ref_price: float,
                      stop_loss: float, tp1, strategy: str,
                      atr: float, balance: float, rs: dict):
    """Return (lot_size, None) when a trade is allowed, or (None, reason)."""
    direction = direction.upper()
    stop_dist = abs(float(ref_price) - float(stop_loss))
    if stop_dist <= 0:
        return None, "stop distance is zero"

    # (E) Stop-width cap — keep scalps tight. Skipped when ATR is unavailable.
    if atr > 0 and stop_dist > atr * RG_MAX_STOP_ATR:
        return None, (
            f"stop {stop_dist:.1f} pts exceeds {RG_MAX_STOP_ATR:.1f}x ATR "
            f"({atr * RG_MAX_STOP_ATR:.1f} pts) — too wide for a scalp"
        )

    # (A) Risk-based position size from risk_per_trade_pct.
    risk_pct = float(rs.get("risk_per_trade_pct", 0.5) or 0.5)
    risk_amt = balance * (risk_pct / 100.0)
    lot      = risk_amt / (stop_dist * CONTRACT_SIZE)

    # (B) Hard ceiling on worst-case loss from max_risk_per_trade_pct.
    max_pct = float(rs.get("max_risk_per_trade_pct", 1.0) or 1.0)
    ceiling = balance * (max_pct / 100.0)
    lot_cap = ceiling / (stop_dist * CONTRACT_SIZE)
    lot     = min(lot, lot_cap, float(rs.get("max_lot_size", 0.10) or 0.10))
    lot     = round(lot, 2)
    if lot < 0.01:
        return None, (
            f"risk-correct size below 0.01 lots for a {stop_dist:.1f} pt stop "
            f"(would exceed {max_pct:.1f}% of ${balance:,.0f}) — trade skipped"
        )

    # (E) Minimum TP1 R:R — only when the strategy honours signal TPs.
    # Reversal Runner and Adaptive Runner deliberately widen SL well beyond
    # TP1 distance — real R:R is measured across the full ladder, not at
    # TP1 alone. Adaptive Runner 2's SL is a flat 10pt distance with zero
    # relationship to the signal's own TP1 target, so the same reasoning
    # applies even more directly.
    if tp1 is not None and strategy not in (
        STRATEGY_CONSERVATIVE_TRIAL,
        STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
    ):
        tp1_dist = abs(float(tp1) - float(ref_price))
        if tp1_dist / stop_dist < RG_MIN_TP1_RR:
            return None, (
                f"TP1 R:R {tp1_dist / stop_dist:.2f}:1 below the "
                f"{RG_MIN_TP1_RR:.2f}:1 minimum"
            )

    # (E) Directional cap — at most 2 unprotected same-direction trades.
    with db_module.db() as conn:
        same_dir = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades "
            "WHERE status='open' AND direction=? AND sl_moved_to_be=0",
            (direction,),
        ).fetchone()[0]
    if same_dir >= 2:
        return None, (
            f"{same_dir} unprotected {direction} trade(s) already open "
            f"(directional cap is 2)"
        )

    return lot, None


def rg_check_halt(rs: dict, balance: float) -> Optional[str]:
    """Return a reason string when the Risk Governor should halt trading.

    balance must be the live MT5 balance -- this used to read
    vantage_simulation_account directly, which is this app's own
    internally-computed P&L ledger and can drift significantly from the
    real account (confirmed 2026-07-07: $707 sim vs $1122 real), producing
    a false drawdown-halt reason based on a number unrelated to the
    account's actual health.
    """
    day_start = rg_day_start_ts()
    with db_module.db() as conn:
        realised_today = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM vantage_simulated_trades "
            "WHERE close_time >= ?", (day_start,),
        ).fetchone()[0] or 0.0

    # (C) Daily loss limit — base off balance before today's realised P&L.
    max_daily = float(rs.get("max_daily_loss_pct", 20.0) or 0)
    if max_daily > 0:
        day_base = balance - realised_today
        limit    = day_base * (max_daily / 100.0)
        if realised_today <= -abs(limit):
            return (
                f"Daily loss limit hit: ${realised_today:.2f} today vs "
                f"-${abs(limit):.2f} ({max_daily:.0f}% of ${day_base:,.0f})"
            )

    # (D) Total account drawdown from peak balance watermark.
    max_total_dd = float(rs.get("max_total_drawdown_pct", 20.0) or 0)
    if max_total_dd > 0 and balance > 0:
        peak_str  = db_module.get_app_config("peak_balance") or "0"
        peak_bal  = float(peak_str or 0)
        if peak_bal > balance:
            dd_pct = (peak_bal - balance) / peak_bal * 100
            if dd_pct >= max_total_dd:
                return (
                    f"Total drawdown {dd_pct:.1f}% from peak ${peak_bal:,.2f} "
                    f"(limit {max_total_dd:.0f}%)"
                )

    return None


def rg_apply_halts_on_close(rs: dict, balance: float) -> None:
    """After a close, trip the pause flag if a circuit breaker fired.

    Wrapped in ONE transaction (fix from 010's finding) -- the original
    made trade_pause_until and risk_halt_reason two separate top-level
    set_app_config() calls, so a crash between them could leave a pause
    flag with no reason. db_module.db() is thread-local and re-entrant, so
    wrapping the whole body here makes both inner set_app_config() calls
    participate in one transaction automatically -- no new adapter needed."""
    reason = rg_check_halt(rs, balance)
    if not reason:
        return
    if reason.startswith("Daily loss"):
        until = rg_day_start_ts() + 86400.0          # resume next broker day
    else:
        cooldown_min = int(rs.get("cooldown_after_loss_min", 15) or 15)
        until = time.time() + cooldown_min * 60
    with db_module.db():
        db_module.set_app_config("trade_pause_until", str(until))
        db_module.set_app_config("risk_halt_reason", reason)
    log.warning("[RG] Trading paused until %.0f: %s", until, reason)

