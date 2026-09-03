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

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.dpm import engine as dpm_engine
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.close_trade import get_trading_balance
from backend.src.services.trading.open_trade import open_trade
from backend.src.services.analytics.reporting import get_open_trades
from backend.src.services.risk.schedule import check_trading_schedule
from backend.src.services.dpm import engine
from backend.src.services.telegram import alerts
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.signals.resolution import _sig_guard_blocks
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
from backend.src.utils.news_calendar import check_news_blackout
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_SCALP_RUNNER, Tick,
)
from backend.src.services.risk import expert_params


def ime_sl_bounds() -> tuple[float, float, float]:
    """(min pts, max pts, ATR multiplier) for the provisional stop an
    instant entry opens with. Were the constants 8.0 / 25.0 / 1.2; now
    Settings > Expert Tunables."""
    return (
        expert_params.get("ime_sl_min_pts"),
        expert_params.get("ime_sl_max_pts"),
        expert_params.get("ime_sl_atr_mult"),
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
    trade_repo.insert_instant_tg_row(
        tg_id, group_id, channel_name, msg.get("sender_name", ""), msg_ts_str,
        text, time.time(), direction, _status,
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
    _ime_sched_ok, _ime_sched_reason = check_trading_schedule(source=channel_name)
    if not _ime_sched_ok:
        log.info("[IME] Instant %s blocked — %s", direction, _ime_sched_reason)
        return

    # News blackout (Trading > News) — needs its own copy here for the same
    # reason the schedule gate above does: this path never calls
    # resolve_open_trade_params(), where the shared gate lives. IME is the
    # fastest path to a live order in the app, which makes it the one that
    # most needs the check.
    _ime_news_ok, _ime_news_reason = check_news_blackout()
    if not _ime_news_ok:
        log.info("[IME] Instant %s blocked — %s", direction, _ime_news_reason)
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
    # Must not override a template-assigned channel (see
    # core_scan_messages_staleness_strategy.py's identical fix/reasoning) --
    # a template fully replaces strategy dispatch by design, and clobbering
    # it here would also defeat the Sig Guard template-detection right below.
    if "high risk" in text.lower() and not ea_templates.is_template_override(strategy):
        log.info("[IME] 'High Risk' flagged in message — using Conservative "
                  "strategy for this trade only")
        strategy = STRATEGY_CONSERVATIVE

    # EA Template Sig Guard — mirrors core_signal_resolution.py's
    # resolve_open_trade_params() check (that function is never reached from
    # this path; IME resolves and opens independently). Without this, IME
    # could open a second template-managed trade for a channel/direction
    # that already has one live, exactly the pile-up Sig Guard exists to
    # prevent on the full-signal path.
    _template_ime = None
    if ea_templates.is_template_override(strategy):
        _tpl_name_ime = ea_templates.template_name_from_override(strategy)
        _template_ime = ea_templates.get_ea_template(_tpl_name_ime)
        if _template_ime is None:
            log.warning("[IME] Template '%s' no longer exists for channel %s — "
                        "skipping instant entry", _tpl_name_ime, channel_name)
            return
        if _template_ime["sig_guard"] and _sig_guard_blocks(channel_name, direction):
            log.info("[IME] Sig Guard: a template-managed trade is already open "
                      "for %s %s — skipping instant entry", channel_name, direction)
            return

    open_trades  = get_open_trades()
    open_count   = len(open_trades)
    max_trades   = int(rs.get("max_open_trades", 1))
    if open_count >= max_trades:
        log.info("[IME] Instant %s — max_trades (%d) reached, skipped", direction, max_trades)
        return
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if _template_ime is not None:
        # Templates size from their own Entries & Lots fields even on the
        # IME path -- this used to hardcode 0.01 (or the global fixed lot)
        # regardless of the template's configured Anchor Lot, same gap as
        # the full-signal and immediate-grid-placement paths. See
        # core_signal_resolution.py's matching fix for the full reasoning.
        _tpl_risk_ime = float(_template_ime.get("risk_pct") or 0)
        if _tpl_risk_ime <= 0:
            lot = min(float(_template_ime.get("lot_anchor") or 0.01),
                     float(rs.get("max_lot_size", 0.10)))
        else:
            lot = 0.01  # placeholder -- the risk_pct>0 branches below recompute it
    else:
        lot = strategy_lot if strategy_lot > 0 else 0.01
    # Use the signalled price as the entry reference if one was provided.
    # Execution is always at market; the price is used for entry_low/high only.
    market_px = tick.ask if direction == "BUY" else tick.bid
    entry_px  = price if price else market_px

    # Provisional emergency SL — replaced when the follow-up signal arrives.
    # 1 lot XAUUSD = 100 oz; P&L per point = lot × 100.
    # Target max loss $150, but clamp between 8 pts (survive spread/noise) and
    # 25 pts (limit damage if the follow-up never arrives).
    if _template_ime is not None:
        # Lot is already resolved from the template's own Entries & Lots
        # fields above -- none of the Risk Governor / fixed-lot / risk_pct
        # branches below may touch it, since each unconditionally
        # recomputes `lot` regardless of strategy. That was the actual bug:
        # a template's Anchor Lot was overwritten by whichever of those
        # three happened to be active, not just by the Risk Governor one.
        #
        # bugs/023: the SL distance used to have the identical problem --
        # always the generic ATR-clamped placeholder below, even when the
        # template itself set sl_pips or use_dynamic_atr, silently
        # overwriting a stop the template's own config was supposed to be
        # authoritative for (same as its TP ladder). Mirrors
        # resolution.py:527-556's precedence exactly: use_dynamic_atr wins
        # over sl_pips, which wins over the ATR-clamped fallback -- measured
        # from entry_px rather than a fresh tick, since every other branch
        # in this function already measures from entry_px and a template
        # fires at market, so the two are effectively the same reference.
        _tpl_sl_dist = None
        if bool(_template_ime.get("use_dynamic_atr")) and dpm_candles:
            try:
                _tpl_atr_ime = dpm_engine.compute_atr(
                    dpm_candles, period=int(_template_ime.get("atr_period") or 14)) or 0.0
            except Exception:
                _tpl_atr_ime = 0.0
            if _tpl_atr_ime > 0:
                _tpl_sl_dist = _tpl_atr_ime * float(_template_ime.get("atr_sl_mult") or 1.5)
        if _tpl_sl_dist is None:
            _tpl_sl_pips = float(_template_ime.get("sl_pips") or 0)
            if _tpl_sl_pips > 0:
                _tpl_sl_dist = _tpl_sl_pips * PIPS_TO_PRICE_XAUUSD
        if _tpl_sl_dist is not None:
            _IME_SL_DIST = round(_tpl_sl_dist, 2)
        else:
            # sl_pips unset (0) -- unchanged from before this fix: the same
            # ATR-clamped placeholder every other IME path uses, for a
            # template that genuinely leaves its stop for the EA/follow-up.
            _ime_atr_tpl = 0.0
            if dpm_candles:
                try:
                    _ime_atr_tpl = dpm_engine.compute_atr(dpm_candles) or 0.0
                except Exception:
                    _ime_atr_tpl = 0.0
            _IME_SL_DIST = max(8.0, min(round(_ime_atr_tpl * 1.2 if _ime_atr_tpl > 0 else 12.0, 2), 25.0))
        provisional_sl = round(
            entry_px - _IME_SL_DIST if direction == "BUY" else entry_px + _IME_SL_DIST, 2
        )
        _ime_max_loss = round(_IME_SL_DIST * lot * 100.0, 2)
    elif bool(rs.get("risk_governor_enabled", 0)):
        # Tier 1 Risk Governor: compute ATR-based provisional SL distance.
        # Lot sizing uses the fixed lot when set; otherwise falls back to risk_pct.
        _rg_atr_ime = 0.0
        if dpm_candles:
            try:
                _rg_atr_ime = dpm_engine.compute_atr(dpm_candles) or 0.0
            except Exception:
                _rg_atr_ime = 0.0
        _lo, _hi, _mult = ime_sl_bounds()
        _IME_SL_DIST = max(_lo, min(round(_rg_atr_ime * _mult if _rg_atr_ime > 0 else 12.0, 2), _hi))
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
        _lo, _hi, _mult = ime_sl_bounds()
        _IME_SL_DIST = max(_lo, min(round(_ime_pts_from_risk, 2), _hi))
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
        _lo, _hi, _mult = ime_sl_bounds()
        _IME_SL_DIST = max(_lo, min(round(_ime_atr_rp * _mult if _ime_atr_rp > 0 else 12.0, 2), _hi))
        _ime_bal_rp  = await get_trading_balance(bridge, starting_balance)
        _ime_risk_rp = float(rs.get("risk_per_trade_pct", 0.5) or 0.5)
        lot = round((_ime_bal_rp * _ime_risk_rp / 100.0) / (_IME_SL_DIST * 100.0), 2)
        lot = max(0.01, min(lot, float(rs.get("max_lot_size", 0.10) or 0.10)))
        provisional_sl = round(
            entry_px - _IME_SL_DIST if direction == "BUY" else entry_px + _IME_SL_DIST, 2
        )
        _ime_max_loss = round(_IME_SL_DIST * lot * 100.0, 2)

    signal_id = str(uuid.uuid4())[:16]
    trade_repo.insert_instant_signal(
        signal_id, channel_name, direction, entry_px, provisional_sl, lot,
        f"Instant market entry — provisional SL ${provisional_sl:.2f} ({_IME_SL_DIST:.1f} pts = -${_ime_max_loss:.0f} max) — awaiting follow-up SL/TP (tg_id={tg_id})",
        time.time(), tg_id,
    )

    try:
        from backend.src.utils import latency_trace as _lt_ime
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
        if _template_ime is not None:
            # bugs/023: instant_followup.py's managed_by == "ea" skip means
            # no follow-up SL/TP is EVER applied to a template-managed
            # trade -- "awaiting follow-up" here was a promise that
            # structurally could not be kept. The SL above is already the
            # template's own, not a placeholder, so say so instead.
            _sl_note = f"_(SL from template \"{_tpl_name_ime}\")_"
        elif _ime_self_managed:
            _sl_note = "_(levels set by strategy immediately)_"
        else:
            _sl_note = f"_(provisional {_IME_SL_DIST:.1f} pts / -${_ime_max_loss:.0f} max — awaiting follow-up)_"
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
            trade_repo.set_trade_levels(
                trade_result["trade_id"], _ime_ex_sl,
                tp1=_ime_ex_tp1, tp2=_ime_ex_tp2)
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
                    from backend.src.services.broker import ea_bridge as _ea_mod
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
                trade_repo.set_trade_levels(
                    trade_result["trade_id"], exact_sl,
                    tp1=ct_tps[0], tp2=ct_tps[1], tp3=ct_tps[2],
                    tp4=ct_tps[3], tp5=ct_tps[4], tp6=ct_tps[5])
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
        trade_repo.delete_failed_instant_signal(signal_id, tg_id)
