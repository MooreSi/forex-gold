"""Signal resolution -- extracted verbatim (no logic changes) from the
FRONT HALF of core/engine.py's SimulationEngine.open_trade_from_signal
(everything from the signal fetch through resolving strategy/lot_size/
stop_loss_to_use), as part of the core/engine.py migration series. See
docs/todo/refactor/core-signal-resolution-migration/020-*.md.

The BACK HALF of open_trade_from_signal (the atomic signal-claim, the call
into open_trade, and 6 strategy-specific POST-fill bridge.modify_order
overrides) is a separate, not-yet-extracted pack -- it mutates a live MT5
order, a different risk class from this pure-computation/read-only half.
See docs/todo/refactor/core-signal-resolution-migration/README.md.

Takes `bridge` explicitly instead of self._bridge/self.get_tick(), and
`dpm_candles` explicitly instead of self._dpm_candles (instance state
refreshed by the, also deferred, _monitor_loop -- not derivable from the
database). Reuses core_risk_governor.check_pre_trade_filters/
price_in_entry_range/rg_size_and_check (pack 1), core_fees_sizing.
suggest_lot_size (pack 1), core_close_trade.get_trading_balance (pack 10).
"""
from __future__ import annotations

from typing import Any, Optional

from forex_trader.core import database as db_module
from backend.src.services.broker import ea_templates as ea_templates
from forex_trader.core.core_close_trade import get_trading_balance
from forex_trader.core.core_fees_sizing import suggest_lot_size
from backend.src.services.risk.governor import check_pre_trade_filters, price_in_entry_range, rg_size_and_check
from backend.src.services.risk.strategy_params import get_strategy_params
from backend.src.services.risk.schedule import check_trading_schedule
from backend.src.utils.models import (
    Tick,
    STRATEGY_SCALE_OUT, STRATEGY_NO_SL_SCALE, STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_TRAIL_STOP, STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2, MAX_TP,
)


def _rr_sl_dist(stated_sl_dist: float) -> float:
    """Reversal Runner SL distance (pts) from the signal's stated SL distance.

    min(sl_mult x stated, sl_cap_pt); falls back to sl_floor_pt if the
    stated distance is missing or looks like bad data (too small/huge —
    same sl_dist<=50 filter used when validating this against historical
    signals). Live-tunable via core_strategy_params (Trading > Strategy >
    Strategy Parameters) — defaults 4x/20pt/8pt, unchanged from the
    original hardcoded values.
    """
    p = get_strategy_params(STRATEGY_REVERSAL_RUNNER)
    if not stated_sl_dist or stated_sl_dist < 0.5 or stated_sl_dist > 50:
        return p["sl_floor_pt"]
    return min(stated_sl_dist * p["sl_mult"], p["sl_cap_pt"])


def _adaptive_sl_dist(stated_sl_dist: float, final_tp_dist: float) -> float:
    """Adaptive Runner SL distance (pts).

    Same widened formula as _rr_sl_dist() (min(sl_mult x stated,
    sl_cap_pt)), but additionally capped at tp_cap_frac of final_tp_dist —
    the distance to the signal's own furthest TP — and never tightened
    below stated_sl_dist itself. If final_tp_dist isn't known (0), falls
    back to the plain Reversal Runner-style widening with no TP-side cap.
    Live-tunable via core_strategy_params — defaults 4x/20pt/8pt/50%,
    unchanged from the original hardcoded values.
    """
    p = get_strategy_params(STRATEGY_ADAPTIVE_RUNNER)
    if not stated_sl_dist or stated_sl_dist < 0.5 or stated_sl_dist > 50:
        return p["sl_floor_pt"]
    widened = min(stated_sl_dist * p["sl_mult"], p["sl_cap_pt"])
    if final_tp_dist > 0:
        tp_cap = final_tp_dist * p["tp_cap_frac"]
        return max(stated_sl_dist, min(widened, tp_cap))
    return widened


def _adaptive_final_tp_dist(sig: dict, entry_mid: float, is_buy: bool) -> float:
    """Distance (pts) from entry_mid to the furthest of the signal's TPs that
    are actually on the correct side of entry_mid — mirrors the same
    correct-side filter engine.py's live _run_tp_ladder already applies, so a
    corrupt/mis-parsed TP value (e.g. a stored tp2 of 40.0 for a fill near
    4048) can't be read as the "final target" and corrupt the SL cap above."""
    dists = [
        abs(float(sig[f"tp{i}"]) - entry_mid)
        for i in range(1, MAX_TP + 1)
        if sig.get(f"tp{i}") is not None
        and ((is_buy and float(sig[f"tp{i}"]) > entry_mid)
             or (not is_buy and float(sig[f"tp{i}"]) < entry_mid))
    ]
    return max(dists, default=0.0)


def _sig_guard_blocks(channel_name: str, direction: str) -> bool:
    """True if a template-managed trade is already open for this channel +
    direction -- Sig Guard blocks a new one from opening alongside it."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT 1 FROM vantage_simulated_trades WHERE status='open' "
            "AND tg_source=? AND direction=? AND strategy LIKE ? LIMIT 1",
            (channel_name, direction.upper(), f"{ea_templates.TEMPLATE_OVERRIDE_PREFIX}%"),
        ).fetchone()
    return row is not None


async def resolve_open_trade_params(
    bridge: Any,
    signal_id: str,
    lot_size_override: Optional[float] = None,
    tick: Optional[Tick] = None,
    age_lot_mult: float = 1.0,
    dpm_candles: Optional[list] = None,
    starting_balance: float = 1000.0,
) -> dict:
    """Resolve a pending/active signal into everything open_trade() needs.
    Returns {sig, strategy, lot_size, stop_loss_to_use, tick}. Raises
    ValueError/RuntimeError on any gate failure, exactly as the original."""
    with db_module.db() as conn:
        sig = db_module.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
        )
    if not sig:
        raise ValueError(f"Signal {signal_id} not found")
    if sig["status"] not in ("pending", "active"):
        raise ValueError(f"Signal is {sig['status']}, cannot open")

    rs = db_module.get_risk_settings()

    # ── Global circuit breaker ───────────────────────────────────────────
    _cb = db_module.get_circuit_breaker_state()
    if _cb["is_active"]:
        remaining_mins = int(_cb["remaining_secs"] // 60) + 1
        _cb_msg = (
            f"Circuit breaker active — live trading blocked. "
            f"Resumes in ~{remaining_mins} min."
        )
        raise ValueError(_cb_msg)

    # Max-open-trades is enforced in open_trade() itself, against whichever
    # node actually executes — see that function for why it moved there.

    # Trading Markets session gate — block live execution outside selected sessions.
    _sess_ok, _sess_name = db_module.is_session_allowed(rs)
    if not _sess_ok:
        raise ValueError(
            f"Trading session '{_sess_name}' is not active in your Trading Markets selection "
            "(Trading > Strategy > Trading Markets)"
        )

    # Trading Schedule gate — per-day/per-window profit-target discipline cap.
    # Automated-only by construction: this function is never reached from the
    # manual market order path (see core_manual_market_order.py).
    _sched_ok, _sched_reason = check_trading_schedule(source="telegram")
    if not _sched_ok:
        raise ValueError(f"Trading Schedule: {_sched_reason} (Trading > Schedule)")

    # Resolve strategy: channel override > auto-Claude rec > global Active Strategy.
    _ch_src_early = sig.get("source_name") or ""
    _ch_override  = db_module.get_channel_strategy_override(_ch_src_early)
    if _ch_override == "auto":
        _rec = db_module.get_channel_strategy_rec(_ch_src_early)
        strategy = _rec.get("strategy") or rs.get("trade_strategy", STRATEGY_SCALE_OUT)
    elif _ch_override:
        strategy = _ch_override
    else:
        strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT)

    # ── EA Template override ────────────────────────────────────────────
    # A template fully replaces strategy dispatch (Trading > Strategy >
    # EA Templates) -- the EA manages the trade end-to-end from the raw
    # signal levels plus the template's own fields, so none of the
    # strategy-specific SL/lot logic below applies. See
    # core_ea_templates.py's module docstring.
    _is_template = ea_templates.is_template_override(_ch_override)
    _template: Optional[dict] = None
    if _is_template:
        _tpl_name = ea_templates.template_name_from_override(_ch_override)
        _template = ea_templates.get_ea_template(_tpl_name)
        if _template is None:
            raise ValueError(f"Template '{_tpl_name}' no longer exists — reassign this channel")
        if _template["sig_guard"] and _sig_guard_blocks(_ch_src_early, sig["direction"]):
            raise ValueError(
                f"Sig Guard: a template-managed trade is already open for "
                f"'{_ch_src_early}' {sig['direction']}"
            )

    # Pre-trade filters: R:R and directional cap.
    # Conservative, Conservative Trial, Trail Stop, Signal Climber, and
    # Reversal Runner skip this — the first three override signal TPs from fill price;
    # the latter two use the full TP ladder (not TP1 alone), and Reversal Runner's SL
    # is deliberately widened past TP1, so the TP1 R:R check is misleadingly low.
    # Adaptive Runner joins them for the same reason. Adaptive Runner 2 also
    # joins: it overrides the signal SL entirely with a fixed 10pt distance,
    # so the TP1 R:R check would be measuring against a stop that isn't
    # actually going to be used. EA Templates join them too — the EA computes
    # its own management independent of the raw TP1 distance.
    _self_level_strategies = (
        STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
        STRATEGY_TRAIL_STOP, STRATEGY_SIGNAL_CLIMBER,
        STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
    )
    if strategy not in _self_level_strategies and not _is_template:
        filter_err = check_pre_trade_filters(
            sig["direction"], float(sig["entry_low"]), float(sig["entry_high"]),
            float(sig["stop_loss"]), sig.get("tp1"),
            source_name=sig.get("source_name", ""),
        )
        if filter_err:
            raise ValueError(filter_err)

    _el  = float(sig["entry_low"])
    _eh  = float(sig["entry_high"])
    _dir = sig["direction"].upper()

    if tick is None:
        # No pre-fetched tick — fetch fresh and verify price is still in zone.
        tick = await bridge.get_tick()
        if not tick:
            raise RuntimeError("No live price available")
        if not price_in_entry_range(_dir, _el, _eh, tick):
            cur = tick.ask if _dir == "BUY" else tick.bid
            raise ValueError(
                f"Price ${cur:.2f} is {'above' if _dir == 'BUY' else 'below'} the entry zone "
                f"${_el:.2f}–${_eh:.2f} for {_dir}. "
                f"Signal remains pending until price returns to zone."
            )
    # When tick is supplied by the caller, zone entry was already verified —
    # skip the redundant fetch and recheck (eliminates bridge round-trip and
    # the race window where price can exit the zone between checks).

    # Spread guard — block during wide-spread news/liquidity events
    _fs = db_module.get_fee_settings()
    _max_spread = float(_fs.get("max_allowed_spread_points", 50.0))
    if tick.spread_points > _max_spread:
        raise ValueError(
            f"Spread too wide: {tick.spread_points:.1f} pts (max {_max_spread:.1f} pts). "
            "Likely a news event — signal stays active for when spread normalises."
        )

    # ── Channel scorecard: pause / adaptive sizing ──────────────────────
    # A channel auto-paused (or manually paused) by the scorecard blocks new
    # trades; otherwise its rolling-performance lot multiplier scales size.
    _ch_src = sig.get("source_name") or ""
    _ch_mult, _ch_paused = db_module.get_channel_lot_mult(_ch_src)
    if _ch_paused:
        raise ValueError(f"Channel '{_ch_src}' is paused by the scorecard — trade skipped")

    lot_size = lot_size_override or sig.get("lot_size")
    if not lot_size:
        risk_pct  = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
        balance   = await get_trading_balance(bridge, starting_balance)
        entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        lot_size  = suggest_lot_size(entry_mid, float(sig["stop_loss"]), balance, risk_pct)
    # Apply the channel multiplier to the risk-derived size (not to a manual
    # fixed override, which the user set deliberately).
    if _ch_mult != 1.0 and not lot_size_override:
        lot_size = lot_size * _ch_mult
    lot_size = max(0.01, round(lot_size, 2))

    # strategy already resolved above the filter check
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if strategy_lot > 0:
        lot_size = strategy_lot

    # Signal age decay: stale signals trade smaller to reflect reduced confidence
    if age_lot_mult < 1.0 and not lot_size_override:
        lot_size = max(0.01, round(lot_size * age_lot_mult, 2))

    # SL override: each strategy may modify the broker SL placed with the order.
    # Lot sizing always uses the signal SL for position-size calculation.
    stop_loss_to_use = float(sig["stop_loss"])
    if _is_template:
        pass  # EA computes its own SL/TP management from the template fields
    elif strategy == STRATEGY_NO_SL_SCALE:
        # ADX > 30 gate: only open Trend Ratchet in confirmed trending conditions
        if dpm_candles:
            from backend.src.services.dpm.engine import compute_adx
            _tr_adx = compute_adx(dpm_candles)
            if _tr_adx < 30:
                raise ValueError(
                    f"Trend Ratchet blocked — ADX {_tr_adx:.1f} < 30 (market not trending). "
                    "Switch to Scale Out or wait for a stronger trend before entering."
                )
        entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        sl_dist   = abs(entry_mid - stop_loss_to_use)
        if sig["direction"].upper() == "BUY":
            stop_loss_to_use = round(entry_mid - sl_dist * 1.5, 2)
        else:
            stop_loss_to_use = round(entry_mid + sl_dist * 1.5, 2)
    elif strategy in (STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER):
        # Fixed-point SL/TP from fill; signal levels ignored after fill.
        # Live-tunable via core_strategy_params (Trading > Strategy).
        _co_sl_pt = get_strategy_params(strategy)["sl_pt"]
        # Place proxy SL at zone-mid ± SL so MT5 accepts the order.
        _co_sign      = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _co_entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        stop_loss_to_use = round(_co_entry_mid - _co_sign * _co_sl_pt, 2)
        # Recompute lot size from the fixed SL (unless user set a fixed lot)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _co_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _co_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_co_entry_mid, stop_loss_to_use, _co_balance, _co_risk_pct), 2
            ))
    elif strategy == STRATEGY_CONSERVATIVE_TRIAL:
        # Pre-fill SL: proxy using zone mid so MT5 order has a valid SL.
        # After fill we overwrite with the exact fill-relative value.
        _ct_entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        _ct_sign      = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _ct_sl_pts    = round(100.0 / (lot_size * 100.0), 1)  # e.g. 10.0 pts at 0.1 lot
        stop_loss_to_use = round(_ct_entry_mid - _ct_sign * _ct_sl_pts, 2)
    elif strategy == STRATEGY_TRAIL_STOP:
        # Pre-fill proxy SL at trail_stop_sl_pts from zone mid — overwritten post-fill.
        _ts_entry_mid    = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        _ts_sign         = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _ts_sl_pts       = float(rs.get("trail_stop_sl_pts", 5.0))
        stop_loss_to_use = round(_ts_entry_mid - _ts_sign * _ts_sl_pts, 2)
        # Recompute lot size from the configured SL (unless fixed lot is set)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _ts_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _ts_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_ts_entry_mid, stop_loss_to_use, _ts_balance, _ts_risk_pct), 2
            ))
    elif strategy == STRATEGY_SIGNAL_CLIMBER:
        # Signal Climber uses the signal's SL exactly; compute lot from that SL.
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _sc_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _sc_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(
                    (float(sig["entry_low"]) + float(sig["entry_high"])) / 2,
                    stop_loss_to_use, _sc_balance, _sc_risk_pct,
                ), 2
            ))
    elif strategy == STRATEGY_REVERSAL_RUNNER:
        # Widen the signal's stated SL (min(4x, 20pt floor 8pt)); TPs are kept
        # exactly as the signal sent them — overwritten with exact fill-relative
        # SL post-fill below. Lot size is computed from the widened distance,
        # not the signal's raw (too-tight) SL.
        _gv_entry_mid    = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        _gv_sign         = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _gv_stated_dist  = abs(_gv_entry_mid - float(sig["stop_loss"]))
        _gv_sl_pt        = _rr_sl_dist(_gv_stated_dist)
        stop_loss_to_use = round(_gv_entry_mid - _gv_sign * _gv_sl_pt, 2)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _gv_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _gv_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_gv_entry_mid, stop_loss_to_use, _gv_balance, _gv_risk_pct), 2
            ))
    elif strategy == STRATEGY_ADAPTIVE_RUNNER:
        # Same widened-SL idea as Reversal Runner, but capped at 50% of the
        # distance to the signal's own final TP — see _adaptive_sl_dist().
        # TPs are kept exactly as the signal sent them — overwritten with
        # exact fill-relative SL post-fill below.
        _ar_entry_mid     = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        _ar_sign          = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _ar_stated_dist   = abs(_ar_entry_mid - float(sig["stop_loss"]))
        _ar_final_tp_dist = _adaptive_final_tp_dist(sig, _ar_entry_mid, _ar_sign > 0)
        _ar_sl_pt         = _adaptive_sl_dist(_ar_stated_dist, _ar_final_tp_dist)
        stop_loss_to_use  = round(_ar_entry_mid - _ar_sign * _ar_sl_pt, 2)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _ar_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _ar_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_ar_entry_mid, stop_loss_to_use, _ar_balance, _ar_risk_pct), 2
            ))
    elif strategy == STRATEGY_ADAPTIVE_RUNNER_2:
        # Fixed SL, full stop -- not derived from the signal's stated SL
        # or its TP spread at all (unlike Adaptive Runner's capped widening).
        # Pre-fill proxy at zone mid, overwritten with the exact fill-relative
        # value post-fill (see core_open_trade_from_signal.py). Live-tunable
        # via core_strategy_params -- default 10pt, unchanged from launch.
        _ar2_entry_mid   = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        _ar2_sign        = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _ar2_sl_pt       = get_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2)["sl_pt"]
        stop_loss_to_use = round(_ar2_entry_mid - _ar2_sign * _ar2_sl_pt, 2)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _ar2_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _ar2_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_ar2_entry_mid, stop_loss_to_use, _ar2_balance, _ar2_risk_pct), 2
            ))

    # ── Tier 1 Risk Governor: authoritative sizing + hard gates ───────────
    if bool(rs.get("risk_governor_enabled", 0)):
        _rg_ref = tick.ask if _dir == "BUY" else tick.bid
        _rg_atr = 0.0
        if dpm_candles:
            try:
                from backend.src.services.dpm import engine as _dpm_rg
                _rg_atr = _dpm_rg.compute_atr(dpm_candles) or 0.0
            except Exception:
                _rg_atr = 0.0
        _rg_bal = await get_trading_balance(bridge, starting_balance)
        _rg_lot, _rg_err = rg_size_and_check(
            direction=_dir, ref_price=_rg_ref, stop_loss=stop_loss_to_use,
            tp1=sig.get("tp1"), strategy=strategy, atr=_rg_atr,
            balance=_rg_bal, rs=rs,
        )
        if _rg_err:
            raise ValueError(f"Risk Governor blocked trade: {_rg_err}")
        # Fixed lot always wins — RG is authoritative only when no fixed lot is set.
        lot_size = strategy_lot if strategy_lot > 0 else _rg_lot

    return {
        "sig": sig,
        "strategy": strategy,
        "lot_size": lot_size,
        "stop_loss_to_use": stop_loss_to_use,
        "tick": tick,
        "is_template": _is_template,
        "template": _template,
    }
