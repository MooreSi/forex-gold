"""Instant Market Entry (IME) opening flow -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._process_instant_entry, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-instant-entry-migration/020-*.md.

Calls core_open_trade.open_trade (pack 11) -- a real MT5 market-order
placement, unchanged from the original. This module places no order
itself; it only calls whatever `bridge` its caller supplies, via
open_trade.

Reuses core_close_trade.get_trading_balance (pack 10) and
core_trade_reporting.get_open_trades (already extracted). dpm_engine is an
already-extracted, stable, pure module for ATR computation -- called
through exactly as the original did.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import dpm_engine
from forex_trader.core import telegram_alerts
from forex_trader.core.core_close_trade import get_trading_balance
from forex_trader.core.core_open_trade import open_trade
from forex_trader.core.core_trade_reporting import get_open_trades
from forex_trader.core.core_trading_schedule import check_trading_schedule
from forex_trader.core.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_SCALP_RUNNER, Tick,
)

log = logging.getLogger(__name__)

# ── Conservative strategy fixed levels (points from fill) ──────────────────────
_CONSERVATIVE_SL_PT    = 5.0
_CONSERVATIVE_TP1_PT   = 3.0

# ── Scalp Runner strategy fixed levels (points from fill) ──────────────────────
_SCALP_RUNNER_SL_PT     = 10.0
_SCALP_RUNNER_TP1_PT    = 3.0
_SCALP_RUNNER_TP2_PT    = 4.0


async def process_instant_entry(
    msg: dict,
    tg_id: str,
    group_id: str,
    channel_name: str,
    text: str,
    direction: str,
    price: Optional[float],
    rs: dict,
    auto_execute: bool,
    bridge: Any,
    dpm_candles: list,
    starting_balance: float = 1000.0,
) -> None:
    """Open an immediate market order for a bare 'XAU Buy/Sell Now' message."""
    msg_ts_str = msg.get("timestamp") or ""

    # Staleness guard — instant entries must be acted on within 4 minutes,
    # matching the normal-signal window: on restart the backfill re-reads
    # recent channel history; without a tight window an old "Buy Now"
    # message would fire a live trade at a price the signal never meant.
    _MAX_INSTANT_AGE = 4 * 60  # seconds
    is_stale = False
    if msg_ts_str:
        try:
            from datetime import datetime as _dt, timezone as _tz
            _tg_dt = _dt.fromisoformat(msg_ts_str.replace("Z", "+00:00"))
            if _tg_dt.tzinfo is None:
                _tg_dt = _tg_dt.replace(tzinfo=_tz.utc)
            if time.time() - _tg_dt.timestamp() > _MAX_INSTANT_AGE:
                is_stale = True
        except Exception:
            pass
    # If no timestamp is available we cannot verify freshness — treat as stale
    # to be safe (a genuine live message always carries a timestamp).
    else:
        is_stale = True

    _status = "instant_historical" if is_stale else "instant_pending"
    with db_module.db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO vantage_tg_signals
               (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, group_id, channel_name, msg.get("sender_name", ""), msg_ts_str,
             text, time.time(),
             direction, None, None, None,
             None, None, None, None, None, None, None, None,
             _status),
        )

    if is_stale:
        log.debug("[IME] Stale instant tg_id=%s — recorded only", tg_id)
        return
    if not auto_execute:
        log.info("[IME] Instant %s detected — auto-execute OFF", direction)
        return

    _ime_sess_ok, _ime_sess_name = db_module.is_session_allowed(rs)
    if not _ime_sess_ok:
        log.info("[IME] Instant %s blocked — %s market disabled", direction, _ime_sess_name)
        return

    # Trading Schedule gate — previously only reached via resolve_open_trade_params(),
    # which this fully-automated path never calls; confirmed live 2026-07-23 that
    # a hit profit target did not stop new IME trades. Same check, same place in
    # the flow as the session gate just above.
    _ime_sched_ok, _ime_sched_reason = check_trading_schedule(source="telegram")
    if not _ime_sched_ok:
        log.info("[IME] Instant %s blocked — %s", direction, _ime_sched_reason)
        return

    tick = await bridge.get_tick()
    if not tick:
        log.warning("[IME] Instant %s — no live price, skipped", direction)
        return

    # Spread guard — block instant entries during wide-spread news/spike events
    _fs = db_module.get_fee_settings()
    _max_spread = float(_fs.get("max_allowed_spread_points", 50.0))
    if tick.spread_points > _max_spread:
        log.info("[IME] Instant %s blocked — spread %.1f pts > max %.1f pts",
                 direction, tick.spread_points, _max_spread)
        return

    strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
    # Apply per-channel strategy override — same priority as the full signal path.
    # At IME time we have no full parsed signal to give to per-signal AI eval,
    # so for "auto" channels we fall back to the last Claude recommendation.
    _ch_ov_ime = db_module.get_channel_strategy_override(channel_name)
    if _ch_ov_ime == "auto":
        _ch_rec_ime = db_module.get_channel_strategy_rec(channel_name)
        strategy = _ch_rec_ime.get("strategy") or strategy
        log.info("[IME] Channel %s auto-strategy → %s (last rec)", channel_name, strategy)
    elif _ch_ov_ime:
        strategy = _ch_ov_ime
        log.info("[IME] Channel %s strategy override → %s", channel_name, strategy)
    # Per-signal "High Risk" override — same rule as the full-signal path.
    if "high risk" in text.lower():
        log.info("[IME] 'High Risk' flagged in message — using Conservative "
                  "strategy for this trade only")
        strategy = STRATEGY_CONSERVATIVE

    open_trades  = get_open_trades()
    open_count   = len(open_trades)
    max_trades   = int(rs.get("max_open_trades", 1))
    if open_count >= max_trades:
        log.info("[IME] Instant %s — max_trades (%d) reached, skipped", direction, max_trades)
        return
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    lot          = strategy_lot if strategy_lot > 0 else 0.01
    # Use the signalled price as the entry reference if one was provided.
    # Execution is always at market; the price is used for entry_low/high only.
    market_px = tick.ask if direction == "BUY" else tick.bid
    entry_px  = price if price else market_px

    # Provisional emergency SL — replaced when the follow-up signal arrives.
    # 1 lot XAUUSD = 100 oz; P&L per point = lot × 100.
    # Target max loss $150, but clamp between 8 pts (survive spread/noise) and
    # 25 pts (limit damage if the follow-up never arrives).
    if bool(rs.get("risk_governor_enabled", 0)):
        # Tier 1 Risk Governor: compute ATR-based provisional SL distance.
        # Lot sizing uses the fixed lot when set; otherwise falls back to risk_pct.
        _rg_atr_ime = 0.0
        if dpm_candles:
            try:
                _rg_atr_ime = dpm_engine.compute_atr(dpm_candles) or 0.0
            except Exception:
                _rg_atr_ime = 0.0
        _IME_SL_DIST = max(8.0, min(round(_rg_atr_ime * 1.2 if _rg_atr_ime > 0 else 12.0, 2), 25.0))
        if strategy_lot > 0:
            lot = strategy_lot
        else:
            _rg_bal_ime = await get_trading_balance(bridge, starting_balance)
            _rg_risk    = float(rs.get("risk_per_trade_pct", 0.5) or 0.5)
            _rg_maxr    = float(rs.get("max_risk_per_trade_pct", 1.0) or 1.0)
            _rg_lot_ime = (_rg_bal_ime * _rg_risk / 100.0) / (_IME_SL_DIST * 100.0)
            _rg_cap_ime = (_rg_bal_ime * _rg_maxr / 100.0) / (_IME_SL_DIST * 100.0)
            lot = round(min(_rg_lot_ime, _rg_cap_ime,
                            float(rs.get("max_lot_size", 0.10) or 0.10)), 2)
            if lot < 0.01:
                log.info("[IME] Instant %s skipped — Risk Governor: risk-correct size "
                         "below 0.01 lots for a %.1f pt stop", direction, _IME_SL_DIST)
                return
        provisional_sl = round(
            entry_px - _IME_SL_DIST if direction == "BUY" else entry_px + _IME_SL_DIST, 2
        )
        _ime_max_loss = round(_IME_SL_DIST * lot * 100.0, 2)
    elif strategy_lot > 0:
        # Governor off, fixed lot set: use it. Provisional stop is derived
        # from the $150 max-loss cap and clamped 8-25 pts.
        lot = strategy_lot
        _IME_MAX_RISK_USD = 150.0
        _ime_pts_from_risk = _IME_MAX_RISK_USD / (lot * 100.0)
        _IME_SL_DIST = max(8.0, min(round(_ime_pts_from_risk, 2), 25.0))
        provisional_sl = round(
            entry_px - _IME_SL_DIST if direction == "BUY" else entry_px + _IME_SL_DIST, 2
        )
        _ime_max_loss = round(_IME_SL_DIST * lot * 100.0, 2)
    else:
        # Governor off, fixed lot = 0: size from risk_per_trade_pct, matching
        # the standard signal path. Provisional stop is ATR-based (8-25 pts).
        _ime_atr_rp = 0.0
        if dpm_candles:
            try:
                _ime_atr_rp = dpm_engine.compute_atr(dpm_candles) or 0.0
            except Exception:
                _ime_atr_rp = 0.0
        _IME_SL_DIST = max(8.0, min(round(_ime_atr_rp * 1.2 if _ime_atr_rp > 0 else 12.0, 2), 25.0))
        _ime_bal_rp  = await get_trading_balance(bridge, starting_balance)
        _ime_risk_rp = float(rs.get("risk_per_trade_pct", 0.5) or 0.5)
        lot = round((_ime_bal_rp * _ime_risk_rp / 100.0) / (_IME_SL_DIST * 100.0), 2)
        lot = max(0.01, min(lot, float(rs.get("max_lot_size", 0.10) or 0.10)))
        provisional_sl = round(
            entry_px - _IME_SL_DIST if direction == "BUY" else entry_px + _IME_SL_DIST, 2
        )
        _ime_max_loss = round(_IME_SL_DIST * lot * 100.0, 2)

    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                lot_size,notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Instant ({channel_name})",
             direction, entry_px, entry_px, provisional_sl,
             None, None, None, None, None, None, None, None,
             lot,
             f"Instant market entry — provisional SL ${provisional_sl:.2f} ({_IME_SL_DIST:.1f} pts = -${_ime_max_loss:.0f} max) — awaiting follow-up SL/TP (tg_id={tg_id})",
             "active", time.time(), time.time()),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='instant_activated', signal_id=?"
            " WHERE tg_message_id=?",
            (signal_id, tg_id),
        )

    try:
        from forex_trader.core import latency_trace as _lt_ime
        _lt_ime.mark(tg_id, "t7_decided")
        trade_result = await open_trade(
            bridge, signal_id=signal_id, direction=direction,
            entry_low=entry_px, entry_high=entry_px,
            stop_loss=provisional_sl,
            lot_size=lot, tick=tick, strategy=strategy,
            tg_source=channel_name,
        )
        _lt_ime.mark(tg_id, "t8_ordered")
        exec_price  = float(trade_result.get("entry_price", entry_px))
        _ime_ticket = trade_result.get("mt5_ticket") or "pending"
        log.info("[IME] Instant %s executed @ %.2f lot=%.2f ticket=%s SL=%.2f (provisional)",
                 direction, exec_price, lot, _ime_ticket, provisional_sl)
        _ime_self_managed = strategy in (
            STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL
        )
        _sl_note = (
            "_(levels set by strategy immediately)_"
            if _ime_self_managed else
            f"_(provisional {_IME_SL_DIST:.1f} pts / -${_ime_max_loss:.0f} max — awaiting follow-up)_"
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"*Immediate Signal Entry*  ({telegram_alerts._md_esc(channel_name)})\n"
            f"*{direction}* at ${exec_price:.2f}  |  lot {lot:.2f}  |  ticket `{_ime_ticket}`\n"
            f"SL: ${provisional_sl:.2f} {_sl_note}",
            event_type="instant_entry",
        ))

        # ── Conservative / Scalp Runner: post-fill SL/TP override (IME path) ─
        # open_trade_from_signal() is not called for IME trades, so we apply
        # the same fill-relative override here. Scalp Runner gets its own
        # SL/TP1/TP2 constants (see _SCALP_RUNNER_* above) — no longer
        # shares Conservative's levels.
        _ime_ex_tp2 = None
        if strategy in (STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER) and trade_result.get("trade_id"):
            if strategy == STRATEGY_SCALP_RUNNER:
                _ime_sl_pt, _ime_tp1_pt, _ime_tp2_pt = (
                    _SCALP_RUNNER_SL_PT, _SCALP_RUNNER_TP1_PT, _SCALP_RUNNER_TP2_PT,
                )
            else:
                _ime_sl_pt, _ime_tp1_pt, _ime_tp2_pt = (
                    _CONSERVATIVE_SL_PT, _CONSERVATIVE_TP1_PT, None,
                )
            _ime_sign   = 1.0 if direction.upper() == "BUY" else -1.0
            _ime_ex_sl  = round(exec_price - _ime_sign * _ime_sl_pt, 2)
            _ime_ex_tp1 = round(exec_price + _ime_sign * _ime_tp1_pt, 2)
            _ime_ex_tp2 = (
                round(exec_price + _ime_sign * _ime_tp2_pt, 2)
                if _ime_tp2_pt is not None else None
            )
            with db_module.db() as conn:
                conn.execute(
                    """UPDATE vantage_simulated_trades
                       SET stop_loss=?, tp1=?, tp2=?, tp3=NULL,
                           tp4=NULL, tp5=NULL, tp6=NULL, tp7=NULL, tp8=NULL
                       WHERE trade_id=?""",
                    (_ime_ex_sl, _ime_ex_tp1, _ime_ex_tp2, trade_result["trade_id"]),
                )
            _ime_tkt = trade_result.get("mt5_ticket")
            if _ime_tkt:
                try:
                    await bridge.modify_order(int(_ime_tkt), sl=_ime_ex_sl, tp=None)
                except Exception as _e:
                    log.warning("[%s/ime] modify_order SL sync failed: %s", strategy, _e)
            log.info(
                "[%s/ime] trade_id=%s fill=%.2f SL=%.2f(-%.1fpt) TP1=%.2f(+%.1fpt)%s",
                strategy, trade_result["trade_id"][:8], exec_price,
                _ime_ex_sl, _ime_sl_pt, _ime_ex_tp1, _ime_tp1_pt,
                f" TP2={_ime_ex_tp2:.2f}(+{_ime_tp2_pt:.1f}pt)" if _ime_ex_tp2 is not None else "",
            )
            if trade_result.get("managed_by") == "ea":
                try:
                    from forex_trader.core import ea_bridge as _ea_mod
                    _ea = _ea_mod.get_instance()
                    if _ea is not None:
                        _ime_new_tps = {1: _ime_ex_tp1}
                        if _ime_ex_tp2 is not None:
                            _ime_new_tps[2] = _ime_ex_tp2
                        await _ea.update_trade(trade_result["trade_id"], _ime_new_tps)
                except Exception as _e:
                    log.warning("EA update_trade after %s IME fill failed: %s", strategy, _e)

        # ── Conservative / Scalp Runner: entry notification (IME path) ──────
        # Levels themselves were already computed and applied above (with
        # the correct per-strategy constants) — this just sends the
        # Telegram notification using those same values instead of
        # recomputing a second, conflicting set (previously this block
        # independently hardcoded 5pt/5pt for both strategies, silently
        # overwriting whatever the block above had just set correctly).
        if strategy in (STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER) and trade_result.get("trade_id"):
            _entry_label = "Scalp Runner" if strategy == STRATEGY_SCALP_RUNNER else "Conservative"
            _tp2_line = f"  |  TP2: ${_ime_ex_tp2:.2f}" if _ime_ex_tp2 is not None else ""
            asyncio.create_task(telegram_alerts.send_message(
                f"*{_entry_label} Entry* ({telegram_alerts._md_esc(channel_name)})\n"
                f"{direction} @ ${exec_price:.2f}  |  ticket `{_ime_ticket}`\n"
                f"SL: ${_ime_ex_sl:.2f}  |  TP1: ${_ime_ex_tp1:.2f}{_tp2_line}  "
                f"_(fixed pts from fill — follow-up ignored)_",
                trade_result["trade_id"], "conservative_entry",
            ))

        # ── Conservative Trial: set SL + 6 TPs immediately after fill ────
        elif strategy == STRATEGY_CONSERVATIVE_TRIAL and trade_result.get("trade_id"):
            try:
                _ime_sign  = 1.0 if direction.upper() == "BUY" else -1.0
                _ct_sl_pts = round(100.0 / (lot * 100.0), 1)
                exact_sl   = round(exec_price - _ime_sign * _ct_sl_pts, 2)
                ct_tps     = [
                    round(exec_price + _ime_sign * off, 2)
                    for off in (5.0, 10.0, 14.0, 20.0, 27.0, 35.0)
                ]
                with db_module.db() as conn:
                    conn.execute(
                        """UPDATE vantage_simulated_trades
                           SET stop_loss=?, tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?,
                               tp7=NULL, tp8=NULL
                           WHERE trade_id=?""",
                        (exact_sl,
                         ct_tps[0], ct_tps[1], ct_tps[2],
                         ct_tps[3], ct_tps[4], ct_tps[5],
                         trade_result["trade_id"]),
                    )
                _cot_mt5 = trade_result.get("mt5_ticket")
                if _cot_mt5:
                    try:
                        await bridge.modify_order(int(_cot_mt5), sl=exact_sl, tp=None)
                    except Exception as _me:
                        log.warning(
                            "[conservative_trial/IME] modify_order SL sync failed: %s", _me
                        )
                log.info(
                    "[conservative_trial/IME] trade=%s %s fill=%.2f SL=%.2f TPs=%s",
                    trade_result["trade_id"][:8], direction, exec_price, exact_sl,
                    "/".join(f"{v:.2f}" for v in ct_tps),
                )
                asyncio.create_task(telegram_alerts.send_message(
                    f"*Conservative Trial Entry* ({telegram_alerts._md_esc(channel_name)})\n"
                    f"{direction} @ ${exec_price:.2f}  |  ticket `{_ime_ticket}`\n"
                    f"SL: ${exact_sl:.2f} ({_ct_sl_pts:.1f} pt)  |  "
                    f"TP1: ${ct_tps[0]:.2f}  TP2: ${ct_tps[1]:.2f}  TP3: ${ct_tps[2]:.2f}  "
                    f"_(follow-up ignored)_",
                    trade_result["trade_id"], "conservative_trial_entry",
                ))
            except Exception as _cot_e:
                log.warning("[conservative_trial/IME] level-setting failed: %s", _cot_e)

    except Exception as exc:
        _exc_msg = str(exc)
        if "circuit breaker" in _exc_msg.lower() or "trading paused" in _exc_msg.lower():
            # Risk governor working as designed, not a fault — ERROR-level
            # here would falsely flag intended safety behaviour in logs.
            log.info("[IME] Instant entry blocked: %s", exc)
        else:
            log.error("[IME] Instant entry failed: %s", exc)
        with db_module.db() as conn:
            conn.execute("DELETE FROM vantage_signals WHERE signal_id=?", (signal_id,))
            conn.execute(
                "UPDATE vantage_tg_signals SET status='instant_failed'"
                " WHERE tg_message_id=?",
                (tg_id,),
            )
