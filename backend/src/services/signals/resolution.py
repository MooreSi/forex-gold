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

import logging
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.signals import repo as signals_repo
from backend.src.services.signals import tg_repo
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.trading.close_trade import get_trading_balance
from backend.src.services.trading.fees_sizing import suggest_lot_size
from backend.src.services.risk.governor import check_pre_trade_filters, price_in_entry_range, rg_size_and_check
from backend.src.services.risk.strategy_params import get_strategy_params
from backend.src.services.risk.schedule import check_trading_schedule
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
from backend.src.services.risk.schedule import check_trading_schedule, get_schedule_strategy_override
from backend.src.utils.news_calendar import check_news_blackout
from backend.src.utils.models import (
    Tick,
    STRATEGY_SCALE_OUT, STRATEGY_NO_SL_SCALE, STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_TRAIL_STOP, STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_FIXED_RR,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2, MAX_TP,
)

log = logging.getLogger(__name__)


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


def _sig_guard_blocks(channel_name: str, direction: str,
                      guard_pips: float = 0.0,
                      new_entry: Optional[float] = None) -> bool:
    """True if a template-managed trade is already open for this channel +
    direction -- Sig Guard blocks a new one from opening alongside it.

    guard_pips (2026-08-04, the reference copier's "SIG GUARD: 20p"): when
    >0, only an existing trade whose entry is within this many pips of the
    new one blocks. That is the difference between "never stack on this
    channel at all" (0, the original behaviour and still the default) and
    "don't stack on top of the SAME level, but a genuinely separate setup
    further down the chart is fine" -- which is what the copier does, and
    what makes a 20p vs 25p distinction meaningful. Falls back to the
    all-or-nothing check when no entry price is available to measure from.
    """
    entries = signals_repo.template_trade_open_entries(
        channel_name, direction.upper(),
        f"{ea_templates.TEMPLATE_OVERRIDE_PREFIX}%")
    if not entries:
        return False
    if guard_pips <= 0 or new_entry is None:
        return True
    for existing in entries:
        # A placeholder row that has not filled yet (entry 0) has no price
        # to compare, so treat it as blocking rather than waving it through.
        if existing <= 0:
            return True
        if abs(existing - new_entry) <= guard_pips * PIPS_TO_PRICE_XAUUSD:
            return True
    return False



def _template_lot_is_fixed(is_template: bool, template) -> bool:
    """Is this trade's lot a template's own fixed Anchor Lot?

    Such a lot is a deliberate manual value and must not be scaled by the
    channel multiplier -- the same exemption `lot_size_override` gets.

    Decided from the TEMPLATE, not as a by-product of which sizing branch ran.
    It used to be set only inside `if not lot_size and _is_template ...`, so a
    signal that carried its own lot skipped that branch and left the flag
    False. The Telegram Auto route stores a lot on the signal, so every trade
    it placed on a fixed-anchor template was scaled: 0.10 x 1.3 = 0.13, 30%
    more risk than the owner set (ticket 1925815819 and four others, 2026-09).
    Plain-channel signals carry no lot, take the branch, and were correct --
    which is why it looked occasional.

    `risk_pct > 0` is NOT exempt, deliberately: that path derives the lot from
    account risk, the same as generic sizing, so the multiplier is meant to
    apply to it.
    """
    if not is_template or template is None:
        return False
    try:
        return float(template.get("risk_pct") or 0) <= 0
    except (TypeError, ValueError):
        # An unreadable risk_pct must not silently turn a fixed lot into a
        # scaled one; treat it as the fixed case it almost certainly is.
        return True

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
    sig = signals_repo.get_signal(signal_id)
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
    # manual market order path (see core_manual_market_order.py). ENGINE_
    # SOURCE_KEYS are per-engine, not per-channel -- "Reversal Engine"/
    # "Breakout Engine" are this function's own literal source_name for
    # those engines' signals (see reversal_engine_live_execute.py/
    # breakout_signal_live_execute.py); every other source_name is a
    # Telegram channel, gated (and possibly strategy-overridden) per-channel.
    _ch_src_early = sig.get("source_name") or ""
    _sched_src_key = (
        "reversal_engine" if _ch_src_early == "Reversal Engine" else
        "breakout_engine" if _ch_src_early == "Breakout Engine" else
        _ch_src_early
    )
    _sched_ok, _sched_reason = check_trading_schedule(source=_sched_src_key)
    if not _sched_ok:
        raise ValueError(f"Trading Schedule: {_sched_reason} (Trading > Schedule)")

    # News blackout (Trading > News) -- same automated-only reach as the
    # schedule gate above, so it covers Telegram-copied signals and both
    # engines' signals from this one place. The engines also check it earlier
    # in their own flows, where they can record a per-signal skip status
    # instead of raising; this is the backstop for everything that reaches
    # here by another route.
    _news_ok, _news_reason = check_news_blackout()
    if not _news_ok:
        raise ValueError(f"{_news_reason} (Trading > News)")

    # Resolve strategy: Trading Schedule window override > channel override >
    # auto-Claude rec > global Active Strategy.
    _ch_override  = db_module.get_channel_strategy_override(_ch_src_early)

    # Trading Schedule per-window override (Trading > Schedule) -- when the
    # schedule is enabled and the active window has a strategy/template
    # assigned for this engine or (for Telegram) this specific channel, it
    # wins over the channel's own Channel Strategy pick for as long as that
    # window is active.
    _sched_override = get_schedule_strategy_override(_sched_src_key)
    if _sched_override:
        _ch_override = _sched_override

    if _ch_override == "auto":
        # Auto mode (2026-08-14): the AI/auto-manage layer's current pick for
        # this channel. Three outcomes, in order:
        #
        #   stand_down  -- this channel has no measured edge in the current
        #                  regime (see core_auto_template's mapping), so
        #                  refuse the trade outright rather than fall through
        #                  to some default that would still open it.
        #   a rec       -- use it.
        #   no rec yet  -- fall back to the BACKTESTED baseline for the live
        #                  regime, not the global Active Strategy. Auto must
        #                  behave sensibly before the first AI cycle has run
        #                  (app just started, API unconfigured or down),
        #                  which the old `or rs["trade_strategy"]` did not.
        from backend.src.services.positions import core_auto_template as _auto
        _rec = db_module.get_channel_strategy_rec(_ch_src_early)
        strategy = (_rec.get("strategy") or "").strip()
        # A stored rec outlives the rules that produced it, so it is checked
        # against what Auto may run TODAY rather than trusted. When the
        # built-ins stopped being selectable (2026-08-17) the rows already in
        # the database kept their built-in picks, and this branch went on
        # using them: GOLD DIGGERS INSTITUTIONAL traded "limit_runner" for
        # nearly nine hours afterwards at the global 0.1 lot instead of its
        # template's 0.05, because only an EMPTY rec fell through to the
        # baseline. Treating a no-longer-valid pick the same as a missing one
        # is what makes a change to the vocabulary take effect everywhere.
        if strategy and not _auto.is_valid_auto_choice(strategy):
            _stale = strategy
            strategy = ""
            log.info(
                "[Auto] %s had a stale recommendation %r that Auto can no "
                "longer run — falling back to the backtested baseline",
                _ch_src_early, _stale,
            )
        if _auto.is_stand_down(strategy):
            raise ValueError(
                f"Auto: {_auto.describe_cell(_ch_src_early, _auto.regime_from_candles(dpm_candles))} "
                f"(Trading > Schedule: Auto)"
            )
        if not strategy:
            strategy = _auto.baseline_for(_ch_src_early, _auto.regime_from_candles(dpm_candles))
            if _auto.is_stand_down(strategy):
                raise ValueError(
                    f"Auto: {_auto.describe_cell(_ch_src_early, _auto.regime_from_candles(dpm_candles))} "
                    f"(Trading > Schedule: Auto)"
                )
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
    # Keyed off the RESOLVED strategy, not _ch_override. For a pinned channel
    # those are the same string, but an Auto channel's override is the literal
    # "auto" -- so a template arriving through Auto was not recognised as one
    # here and silently skipped everything in this block: the template's SL
    # authority (below), Sig Guard, the "template no longer exists" check, and
    # the `not _is_template` guard that keeps the global Fixed Lot Size from
    # overwriting a template's own Anchor Lot.
    #
    # It went unnoticed because core_open_trade.py derives its own
    # _is_template from `strategy` and does load the template there, so EA
    # legs still used the template's lots -- the two modules disagreed about
    # what "is a template" meant, and only the later one was right. Auto
    # gained template support on 2026-08-14; this half was never updated with
    # it, which is why an Auto channel and a pinned channel on the SAME
    # template did not trade it the same way.
    _is_template = ea_templates.is_template_override(strategy)
    _template: Optional[dict] = None
    if _is_template:
        _tpl_name = ea_templates.template_name_from_override(strategy)
        _template = ea_templates.get_ea_template(_tpl_name)
        if _template is None:
            raise ValueError(f"Template '{_tpl_name}' no longer exists — reassign this channel")
        if _template["sig_guard"]:
            _sg_pips = float(_template.get("sig_guard_pips") or 0)
            _sg_entry = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
            if _sig_guard_blocks(_ch_src_early, sig["direction"], _sg_pips, _sg_entry):
                _sg_where = (f" within {_sg_pips:.0f} pips of ${_sg_entry:.2f}"
                             if _sg_pips > 0 else "")
                raise ValueError(
                    f"Sig Guard: a template-managed trade is already open for "
                    f"'{_ch_src_early}' {sig['direction']}{_sg_where}"
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
    # Fixed R:R joins them: it replaces both the stop AND the target with
    # its own fixed distances from fill, so a check against the signal's
    # TP1 measures levels this strategy will never use.
    _self_level_strategies = (
        STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
        STRATEGY_TRAIL_STOP, STRATEGY_SIGNAL_CLIMBER, STRATEGY_FIXED_RR,
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
        # Grid templates are a pending-order strategy by construction (they
        # stage resting legs spanning the zone -- core_open_trade.py's
        # zone_low/zone_high handoff), so unlike every market-fill strategy
        # here, price is NOT required to already be in the zone: the resting
        # legs themselves are what waits for it. Without this exemption, the
        # "Open Trade Now" button (and anything else routing a template
        # signal through this function) could never fire a grid template
        # signal until price happened to already be back in its zone --
        # exactly the gap that left grid signals unable to be manually
        # actioned at all (2026-07-28).
        _is_grid_template = _is_template and _template is not None and _template.get("mode") == "grid"
        if not _is_grid_template and not price_in_entry_range(_dir, _el, _eh, tick):
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

    # ── EA Template pre-trade filters ────────────────────────────────────
    # late_guard_pips/max_spread_pips/signal_rr_ratio existed as template
    # fields with no implementation until 2026-08-04. All three default to
    # 0 = off (every template saved before this existed), so this is a
    # no-op unless a template deliberately sets one.
    if _is_template and _template is not None:
        _tpl_max_spread_pips = float(_template.get("max_spread_pips") or 0)
        if _tpl_max_spread_pips > 0 and tick.spread_points > _tpl_max_spread_pips * 10.0:
            raise ValueError(
                f"Template '{_template.get('name', '?')}' Max Spread: spread "
                f"{tick.spread_points:.1f} pts exceeds {_tpl_max_spread_pips:.1f} pips "
                f"({_tpl_max_spread_pips * 10.0:.1f} pts) — signal stays active for when "
                "spread normalises."
            )

        # Beyond-zone guard for grid templates only -- a non-grid template
        # already went through the strict price_in_entry_range check above
        # (grid templates are exempt there by construction, since resting
        # legs are what waits for price; this is that exemption's own,
        # optional, distance cap). 0 (default) leaves the always-fire
        # policy untouched -- this only ever restricts a template that
        # explicitly opts into a cap.
        _tpl_late_guard_pips = float(_template.get("late_guard_pips") or 0)
        _is_grid_tpl_lg = _template.get("mode") == "grid"
        if _tpl_late_guard_pips > 0 and _is_grid_tpl_lg and not price_in_entry_range(_dir, _el, _eh, tick):
            _cur = tick.ask if _dir == "BUY" else tick.bid
            _beyond = (_cur - _eh) if _dir == "BUY" else (_el - _cur)
            if _beyond * 10.0 > _tpl_late_guard_pips:
                raise ValueError(
                    f"Template '{_template.get('name', '?')}' Late Guard: price ${_cur:.2f} is "
                    f"{_beyond * 10.0:.1f} pips beyond the {_dir} zone ${_el:.2f}-${_eh:.2f}, "
                    f"past the {_tpl_late_guard_pips:.1f} pip guard — signal stays active for "
                    "when price returns closer to the zone."
                )

        _tpl_rr = float(_template.get("signal_rr_ratio") or 0)
        if _tpl_rr > 0 and sig.get("tp1"):
            _entry_mid_rr = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
            _rr_risk = abs(_entry_mid_rr - float(sig["stop_loss"]))
            _rr_reward = abs(float(sig["tp1"]) - _entry_mid_rr)
            if _rr_risk > 0 and (_rr_reward / _rr_risk) < _tpl_rr:
                raise ValueError(
                    f"Template '{_template.get('name', '?')}' Signal R:R: this signal's own "
                    f"TP1:SL ratio ({_rr_reward / _rr_risk:.2f}:1) is below the required "
                    f"{_tpl_rr:.2f}:1 — trade skipped."
                )

    # ── Channel scorecard: pause / adaptive sizing ──────────────────────
    # A channel auto-paused (or manually paused) by the scorecard blocks new
    # trades; otherwise its rolling-performance lot multiplier scales size.
    _ch_src = sig.get("source_name") or ""
    _ch_mult, _ch_paused = db_module.get_channel_lot_mult(_ch_src)
    if _ch_paused:
        raise ValueError(f"Channel '{_ch_src}' is paused by the scorecard — trade skipped")

    lot_size = lot_size_override or sig.get("lot_size")
    # Derived from the template itself, so it holds however the lot was
    # obtained -- see _template_lot_is_fixed for what this used to miss.
    _lot_is_template_fixed = _template_lot_is_fixed(_is_template, _template)
    if not lot_size and _is_template and _template is not None:
        # A template's own Entries & Lots fields are authoritative for
        # sizing, not the generic per-strategy path below. Grid mode's
        # resting legs already read tpl_lot_anchor/tpl_lot_pending directly
        # on the EA side (HandleOpenTemplateGrid) and only fall back to
        # whatever this function computes if those are zero -- but single
        # mode reuses the plain market-order path with no such override, so
        # it silently used the generic risk-based/global-fixed-lot size
        # instead of the template's own Anchor Lot. This also fixes the
        # value recorded in the DB placeholder row and reported via
        # Telegram, which was never the true anchor size for grid trades
        # either.
        #
        # risk_pct (0 = OFF) lets a template size itself from account risk
        # instead of a flat lot, same convention as every other strategy's
        # own risk_pct field.
        _tpl_risk_pct = float(_template.get("risk_pct") or 0)
        if _tpl_risk_pct > 0:
            balance   = await get_trading_balance(bridge, starting_balance)
            entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
            lot_size  = suggest_lot_size(entry_mid, float(sig["stop_loss"]), balance, _tpl_risk_pct)
        else:
            # Global parameters still apply as a ceiling even though the
            # template's fixed lot is the primary source -- suggest_lot_size
            # would clamp to this too, but the raw-anchor-lot path bypasses
            # that function entirely so it needs its own cap.
            _max_lot = float(rs.get("max_lot_size", 0.10))
            lot_size = min(float(_template.get("lot_anchor") or 0.01), _max_lot)
            # _lot_is_template_fixed is already True: set above from the
            # template, so it holds whether or not this branch ran.
    if not lot_size:
        risk_pct  = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
        balance   = await get_trading_balance(bridge, starting_balance)
        entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        lot_size  = suggest_lot_size(entry_mid, float(sig["stop_loss"]), balance, risk_pct)
    # Apply the channel multiplier to the risk-derived size (not to a manual
    # fixed override, which the user set deliberately). A template's own
    # fixed Anchor Lot (risk_pct == 0 branch above) is the same kind of
    # deliberate manual value as lot_size_override -- scaling it silently
    # (e.g. Reversal Engine's 1.3x -> 0.1 becoming 0.13) defeats the point
    # of setting a fixed lot on the template. The risk_pct>0 template branch
    # is excluded from this exemption: that path is genuinely risk-derived,
    # same as the generic non-template sizing below, so the multiplier is
    # meant to apply there.
    if _ch_mult != 1.0 and not lot_size_override and not _lot_is_template_fixed:
        lot_size = lot_size * _ch_mult
    lot_size = max(0.01, round(lot_size, 2))

    # strategy already resolved above the filter check
    #
    # The global fixed-lot override does NOT apply to templates: a template
    # is a self-contained, per-channel sizing definition (Anchor Lot/Pending
    # Lot), and letting one global toggle silently overwrite that would
    # defeat the entire point of those fields being on the template at all.
    # Every other strategy keeps "fixed lot always wins".
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if strategy_lot > 0 and not _is_template:
        lot_size = strategy_lot

    # Signal age decay: stale signals trade smaller to reflect reduced confidence
    if age_lot_mult < 1.0 and not lot_size_override:
        lot_size = max(0.01, round(lot_size * age_lot_mult, 2))

    # SL override: each strategy may modify the broker SL placed with the order.
    # Lot sizing always uses the signal SL for position-size calculation.
    stop_loss_to_use = float(sig["stop_loss"])
    if _is_template:
        # A template's own SL (sl_pips) is meant to be as authoritative as
        # its TP ladder (core_ea_templates.py's tp{n}_pips -- "replacing the
        # signal's own TP prices entirely rather than only filling gaps"),
        # not merely a fallback for when the signal happens to carry none.
        # This was a no-op unconditionally, though, so a channel's own
        # signal generator (Reversal/Breakout/Bounce all compute their own
        # structure/ATR-based stop) silently kept its variable distance no
        # matter what sl_pips said -- confirmed live: "Asian - Grid"
        # (sl_pips=50) trades opening with whatever distance the triggering
        # Reversal Engine signal happened to carry instead of a fixed 50.
        # Computed from the same price reference resolve_template_tps()
        # uses for the TP ladder, so SL and TP measure from the same entry
        # reference. sl_pips=0 (unset) still defers to the signal's own
        # stop, unchanged.
        #
        # use_dynamic_atr (2026-08-04 -- existed as a template field with no
        # implementation): "sl_pips is ignored in favour of ATR x
        # atr_sl_mult" per its own comment, when candle data is available to
        # compute one. Falls back to sl_pips (and, failing that, the
        # signal's own stop) if it isn't.
        _tpl_sl_dist = None
        if _template and bool(_template.get("use_dynamic_atr")) and dpm_candles:
            from backend.src.services.dpm.engine import compute_atr
            _tpl_atr = compute_atr(dpm_candles, period=int(_template.get("atr_period") or 14)) or 0.0
            if _tpl_atr > 0:
                _tpl_sl_dist = _tpl_atr * float(_template.get("atr_sl_mult") or 1.5)
        if _tpl_sl_dist is None:
            _tpl_sl_pips = float(_template.get("sl_pips") or 0) if _template else 0.0
            if _tpl_sl_pips > 0:
                _tpl_sl_dist = _tpl_sl_pips * PIPS_TO_PRICE_XAUUSD
        if _tpl_sl_dist is not None:
            _tpl_sl_ref = tick.ask if _dir == "BUY" else tick.bid
            stop_loss_to_use = round(
                _tpl_sl_ref - _tpl_sl_dist if _dir == "BUY" else _tpl_sl_ref + _tpl_sl_dist,
                2,
            )
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
    elif strategy == STRATEGY_FIXED_RR:
        # Fixed R:R -- stop and target are both fixed distances from the
        # fill, and both go to the broker, so the signal's own SL/TP are
        # used for nothing but the entry zone.
        #
        # Recomputing the lot from that fixed stop is the point, not a
        # side effect: it makes risk per trade constant instead of a
        # function of whatever stop distance the signal happened to carry.
        # Measured 2026-07-28, realised risk across one day ranged $4.87
        # to $300 (0.5%-32.6% of balance) against a configured 0.5%,
        # because a fixed lot decouples size from stop distance entirely.
        _fr_sl_pt     = get_strategy_params(strategy)["sl_pt"]
        _fr_sign      = 1.0 if sig["direction"].upper() == "BUY" else -1.0
        _fr_entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
        stop_loss_to_use = round(_fr_entry_mid - _fr_sign * _fr_sl_pt, 2)
        if not (float(rs.get("strategy_lot_size", 0)) > 0) and not lot_size_override and not sig.get("lot_size"):
            _fr_risk_pct = float(sig.get("risk_pct") or rs.get("risk_per_trade_pct", 0.5))
            _fr_balance  = await get_trading_balance(bridge, starting_balance)
            lot_size = max(0.01, round(
                suggest_lot_size(_fr_entry_mid, stop_loss_to_use, _fr_balance, _fr_risk_pct), 2
            ))
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
