"""Auto-execution flow -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine._scan_messages (lines 6984-7364), as
part of the core/engine.py migration series. See
docs/todo/refactor/core-scan-messages-auto-execute-migration/020-*.md.

Highest real-money surface in the whole migration series: places a real
MT5 order via `open_trade` (already extracted -- reused, not re-derived)
and, for Conservative/Scalp Runner, follows up with a `modify_order`
SL/TP sync and an EA `update_trade` call on a live ticket.

`get_open_trades_fn`/`find_and_apply_instant_followup_fn`/
`check_pre_trade_filters_fn`/`suggest_lot_size_fn`/`get_trading_balance_fn`
are required explicit collaborators bound to the same simplified call
shapes `_scan_messages` itself uses -- small/already-extracted helpers out
of scope for this pack (see the parent
`core-scan-messages-migration/README.md`). `open_trade_fn` defaults to the
real, already-extracted `core_open_trade.open_trade`.

The instant-followup-matched early return carries a `followup_matched: True`
key (added at core-engine-wiring time, not part of the original verbatim
extraction) -- the original inline code did `continue` immediately in this
branch, skipping the "signal detected" Telegram alert `_scan_messages` sends
for every other outcome (the followup path already sends its own, more
specific notification via `find_and_apply_instant_followup_fn`). Without
this flag, a caller delegating straight to this function would have no way
to tell "instant followup matched, skip the alert" apart from "trade opened
normally, send the alert" -- both return `executed: True` -- and would
introduce a duplicate/misleading Telegram alert on every IME-followup match.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import ea_bridge
from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core.core_open_trade import open_trade as _real_open_trade
from forex_trader.core.core_strategy_params import get_strategy_params
from forex_trader.core.core_trading_schedule import check_trading_schedule
from forex_trader.core.news_calendar import check_news_blackout
from forex_trader.core.models import (
    Tick,
    STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_FIXED_RR,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
)
from forex_trader.core.signal_parser import validate_signal

log = logging.getLogger(__name__)

# Conservative/Scalp Runner/Adaptive Runner 2's fixed point levels are
# live-tunable via core_strategy_params (Trading > Strategy > Strategy
# Parameters) -- see get_strategy_params() calls at each use site below.

_SELF_MANAGED_STRATEGIES = {STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER,
                            STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_FIXED_RR}
_CLIMBER_MODE_STRATEGIES = (STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER,
                             STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2)
_PRE_TRADE_FILTER_BYPASS_STRATEGIES = _SELF_MANAGED_STRATEGIES | set(_CLIMBER_MODE_STRATEGIES)
_SL_OVERRIDE_STRATEGIES = (STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER)


def price_in_entry_range(direction: str, entry_low: float, entry_high: float, tick: Tick) -> bool:
    if direction.upper() == "BUY":
        return tick.ask <= entry_high
    else:
        return tick.bid >= entry_low


def ime_enabled_for_channel(rs: dict, channel_name: str) -> bool:
    """True when Immediate Market Entry is live for `channel_name`.

    Mirrors engine.py's own IME gate exactly: the global risk-settings
    toggle AND the per-channel instant_entry_enabled flag, whose default
    depends on the channel's parser format (format_ab/gd2 default ON,
    everything else OFF) -- so a channel that has never been configured
    reads the same here as it does there. engine.py keeps its own inline
    copy because it already has `ch_cfg` loaded in that loop; this one
    exists for callers that only have the channel name.
    """
    if not bool(rs.get("immediate_market_entry", 0)):
        return False
    ch_cfg = db_module.get_channel_parser_config(channel_name) or {}
    _default = 1 if ch_cfg.get("parser_format") in ("format_ab", "gd2") else 0
    return bool(ch_cfg.get("instant_entry_enabled", _default))


async def execute_auto_signal(
    parsed: dict,
    tg_id: str,
    channel_name: str,
    source_label: str,
    strategy: str,
    rs: dict,
    sess_ok: bool,
    per_signal_skip: bool,
    per_signal_skip_reason: str,
    skip_reason: str,
    bridge: Any,
    get_open_trades_fn: Callable[[], list],
    find_and_apply_instant_followup_fn: Callable[[str, str, dict, str], Awaitable[bool]],
    check_pre_trade_filters_fn: Callable[..., Optional[str]],
    suggest_lot_size_fn: Callable[[float, float, float, float], float],
    get_trading_balance_fn: Callable[[], Awaitable[float]],
    open_trade_fn: Optional[Callable[..., Awaitable[dict]]] = None,
) -> dict:
    """Returns {'executed', 'exec_lot', 'exec_price', 'trade_result',
    'skip_reason', 'gap_note'}."""
    if open_trade_fn is None:
        open_trade_fn = lambda **kw: _real_open_trade(bridge, **kw)

    executed = False
    exec_lot = None
    exec_price = None
    trade_result = None
    gap_note = ""

    # ── Instant trade follow-up ──────────────────────────────────
    # Fixed 2026-07-24: IME follow-up matching must only ever apply to a
    # market-order-shaped follow-up (the normal "XAU USD BUY NOW" -> full
    # zone-signal pattern this feature exists for) -- never to a message
    # that is itself a genuine "BUY/SELL [LIMITS] GOLD @ .../... AREA"
    # Limit Runner/pending-order signal (parsed["tp_open"] is only ever
    # present, True or False, on that format's own return dict -- see
    # signal_parser.parse_limit_order_signal). Without this guard, a
    # template-configured channel sending a bare IME trigger followed by a
    # genuine Limit signal for a *different* setup had the Limit signal
    # silently swallowed as a "follow-up" and applied to the already-open
    # instant-entry market trade instead of ever placing the resting order
    # it actually described.
    if bool(rs.get("immediate_market_entry", 0)) and parsed.get("tp_open") is None:
        followup_matched = await find_and_apply_instant_followup_fn(
            channel_name, parsed["direction"], parsed, tg_id,
        )
        if followup_matched:
            return {"executed": True, "exec_lot": None, "exec_price": None,
                    "trade_result": None, "skip_reason": skip_reason, "gap_note": "",
                    "followup_matched": True}

    # ── Normal open-new-trade flow ───────────────────────────────
    # Trading Schedule gate (2026-08-06). This path opens via
    # core_open_trade.open_trade directly and never calls
    # resolve_open_trade_params, which is where the schedule gate lives for
    # every other route -- so a fresh Telegram signal executed regardless of
    # the schedule, while queued zone-fills, pending-order fills, IME trades
    # and the internal engines were all correctly blocked. Confirmed live
    # 2026-08-06: ticket 1720148940 (Gold Diggers VIP) opened at 12:25 local
    # against a schedule whose last window that day ended at 12:00.
    #
    # Same check, same `source=<channel>` form, and the same position in the
    # flow as core_instant_entry.py's own copy -- that path had this exact
    # gap patched for it in 2026-07-23 and this one was simply missed.
    #
    # Deliberately placed BELOW the IME follow-up block above: a follow-up
    # applies SL/TP to an ALREADY-OPEN trade rather than opening anything,
    # and blocking it would strand that position on its provisional stop --
    # strictly worse than letting it complete.
    _sched_ok, _sched_reason = check_trading_schedule(source=channel_name)
    # News blackout (Trading > News) -- placed alongside the schedule gate for
    # the same reason and with the same reach, and deliberately also BELOW the
    # IME follow-up block above: a follow-up only applies SL/TP to an
    # already-open trade, and blocking it would strand that position on its
    # provisional stop going into the news event -- the opposite of protective.
    _news_ok, _news_reason = check_news_blackout()
    open_count = len(get_open_trades_fn())
    max_trades = int(rs.get("max_open_trades", 1))
    if not sess_ok:
        pass  # skip_reason already set by the caller
    elif not _sched_ok:
        skip_reason = f"Auto-execution skipped — Trading Schedule: {_sched_reason}"
        log.info("[%s] Signal blocked by Trading Schedule: %s",
                 source_label, _sched_reason)
    elif not _news_ok:
        skip_reason = f"Auto-execution skipped — {_news_reason}"
        log.info("[%s] Signal blocked by news blackout: %s",
                 source_label, _news_reason)
    elif per_signal_skip:
        skip_reason = f"Auto-eval declined signal: {per_signal_skip_reason}"
    elif open_count >= max_trades:
        skip_reason = f"Auto-execution skipped — max open trades ({max_trades}) reached."
    else:
        tick = await bridge.get_tick()
        if not tick:
            skip_reason = "Auto-execution skipped — no live price available."
        else:
            self_mgd = strategy in _SELF_MANAGED_STRATEGIES
            climber_mode = strategy in _CLIMBER_MODE_STRATEGIES
            if self_mgd:
                errs = (
                    ["entry_low must be <= entry_high"]
                    if float(parsed["entry_low"]) > float(parsed["entry_high"])
                    else []
                )
            else:
                errs = validate_signal(
                    parsed["direction"], parsed["entry_low"], parsed["entry_high"],
                    parsed["stop_loss"],
                    parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
                    parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
                )
            if errs:
                skip_reason = f"Auto-execution skipped — signal validation failed: {'; '.join(errs)}"
            else:
                dir_up = parsed["direction"].upper()
                el = float(parsed["entry_low"])
                eh = float(parsed["entry_high"])
                live_px = tick.ask if dir_up == "BUY" else tick.bid
                zone_mid = (el + eh) / 2.0
                in_zone = price_in_entry_range(dir_up, el, eh, tick)

                zone_broken = (
                    (dir_up == "BUY" and live_px < el) or
                    (dir_up == "SELL" and live_px > eh)
                )
                if zone_broken:
                    skip_reason = (
                        f"Auto-execution skipped — {dir_up} zone ${el:.2f}–${eh:.2f} "
                        f"already breached (price ${live_px:.2f}); setup invalidated."
                    )
                    log.info("[%s] Signal rejected — zone breached: %s", source_label, skip_reason)
                else:
                    rr_ref_px = live_px if in_zone else zone_mid
                    # EA Templates join the bypass list (2026-08-05). Every
                    # other execution path already exempts them -- see
                    # core_signal_resolution.resolve_open_trade_params
                    # ("not _is_template") and core_pending_signal_activation
                    # (_grid_tpl) -- for the reason spelled out there: a
                    # template replaces the signal's own SL/TPs with its own
                    # sl_pips/tp*_pips, so scoring the signal's TP1 against a
                    # stop the trade will never use declines trades on numbers
                    # the EA never sees. This path was the only one still
                    # missing it, and `strategy` here is the raw override
                    # string ("template:<name>"), which can never appear in
                    # _PRE_TRADE_FILTER_BYPASS_STRATEGIES (built from built-in
                    # strategy keys) -- so the check silently never matched.
                    # Confirmed live: a GOLD DIGGERS INSTITUTIONAL "BUY LIMITS
                    # ... AREA" signal was rejected at 0.53:1, measured against
                    # a 4229 stop the template itself had just derived from its
                    # 60 sl_pips, and never reached the grid-placement branch
                    # below that would have staged the resting legs.
                    #
                    # Immediate Market Entry joins the bypass too (2026-08-06,
                    # explicit user directive). IME means the user has opted
                    # into taking this channel's fill at market the moment the
                    # signal lands; an R:R gate measured against the live price
                    # contradicts that directly, since by construction IME
                    # fires when price is already wherever it is rather than
                    # waiting for a better point in the zone. Confirmed live:
                    # a GOLD DIGGERS INSTITUTIONAL SELL 4258-4263 (SL 4269,
                    # TP1 4256) was declined at 0.33:1 measured from a live bid
                    # of 4259.30 -- inside its own zone, but at the end of it
                    # that leaves almost no room to TP1.
                    if (strategy in _PRE_TRADE_FILTER_BYPASS_STRATEGIES
                            or ea_templates.is_template_override(strategy)
                            or ime_enabled_for_channel(rs, channel_name)):
                        filter_err = None
                    else:
                        filter_err = check_pre_trade_filters_fn(
                            parsed["direction"], el, eh,
                            float(parsed["stop_loss"]), parsed.get("tp1"),
                            actual_price=rr_ref_px, source_name=channel_name,
                        )
                    if filter_err:
                        skip_reason = f"Auto-execution skipped — {filter_err}"
                        log.info("[%s] Signal filtered: %s", source_label, filter_err)
                    else:
                        signal_id = str(uuid.uuid4())[:16]
                        balance = await get_trading_balance_fn()
                        entry_mid = (float(parsed["entry_low"]) + float(parsed["entry_high"])) / 2

                        # EA Templates size from their own Entries & Lots fields, not
                        # the generic risk-based path below -- see the matching fix
                        # in core_signal_resolution.py for the full reasoning. This
                        # is a SEPARATE code path (the immediate-placement branch for
                        # a freshly-arrived Telegram signal), so it needs its own
                        # copy of the same logic rather than falling through to it.
                        _tpl_for_sizing = None
                        if ea_templates.is_template_override(strategy):
                            _tpl_for_sizing = ea_templates.get_ea_template(
                                ea_templates.template_name_from_override(strategy))
                        if _tpl_for_sizing is not None:
                            _tpl_risk_pct = float(_tpl_for_sizing.get("risk_pct") or 0)
                            if _tpl_risk_pct > 0:
                                lot = suggest_lot_size_fn(
                                    entry_mid, float(parsed["stop_loss"]), balance, _tpl_risk_pct)
                            else:
                                _max_lot = float(rs.get("max_lot_size", 0.10))
                                lot = min(float(_tpl_for_sizing.get("lot_anchor") or 0.01), _max_lot)
                        else:
                            lot = suggest_lot_size_fn(entry_mid, float(parsed["stop_loss"]),
                                                      balance, float(rs.get("risk_per_trade_pct", 0.5)))
                            strategy_lot = float(rs.get("strategy_lot_size", 0))
                            if strategy_lot > 0:
                                lot = strategy_lot

                        direction = parsed["direction"]
                        el = float(parsed["entry_low"])
                        eh = float(parsed["entry_high"])
                        in_range = price_in_entry_range(direction, el, eh, tick)
                        cur_px = tick.ask if direction == "BUY" else tick.bid

                        # ── EA Template, grid mode (2026-07-28) ──────────────────
                        # A grid template IS a pending-order strategy by
                        # construction -- it stages resting BuyLimit/SellLimit
                        # legs, exactly like Limit Runner's own genuine pending
                        # order -- so it must place immediately, spanning the
                        # signal's own stated zone, rather than defer to the
                        # "insert a pending row, wait for price to re-enter the
                        # zone, then market-fill" path every other strategy
                        # (including single-mode templates, which really are
                        # market-fill strategies) uses below. Without this, a
                        # grid template's signal sat queued waiting for price to
                        # already be back in the zone before ever placing a
                        # broker order at all -- confirmed live 2026-07-27: three
                        # GOLD DIGGERS INSTITUTIONAL signals expired unfilled in
                        # a row this way. The resting grid legs
                        # (core_open_trade.py's zone_low/zone_high handoff, EA's
                        # ApplyGroupTpAction/HandleOpenTemplateGrid) ARE the
                        # "wait for price" mechanism now -- MT5 itself watches
                        # for the fill, not this Python poll. No gap adjustment,
                        # no queue-and-wait: the zone IS the resting order.
                        _tpl_grid = False
                        if ea_templates.is_template_override(strategy):
                            _tpl = ea_templates.get_ea_template(
                                ea_templates.template_name_from_override(strategy))
                            _tpl_grid = bool(_tpl) and _tpl.get("mode") == "grid"

                        if _tpl_grid:
                            with db_module.db() as conn:
                                conn.execute(
                                    """INSERT INTO vantage_signals
                                       (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                                        tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                                        lot_size,notes,status,created_at,activated_at)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (signal_id, f"Telegram Auto ({source_label})",
                                     direction, el, eh, float(parsed["stop_loss"]),
                                     parsed["tp1"], parsed["tp2"], parsed["tp3"],
                                     parsed["tp4"], parsed["tp5"],
                                     parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
                                     lot,
                                     f"Grid template pending order from Telegram {tg_id} ({source_label})",
                                     "active", time.time(), time.time()),
                                )
                                conn.execute(
                                    "UPDATE vantage_tg_signals SET status='activated',signal_id=?"
                                    " WHERE tg_message_id=?",
                                    (signal_id, tg_id),
                                )
                            try:
                                trade_result = await open_trade_fn(
                                    signal_id=signal_id, direction=direction,
                                    entry_low=el, entry_high=eh,
                                    stop_loss=float(parsed["stop_loss"]),
                                    tp1=parsed["tp1"], tp2=parsed["tp2"], tp3=parsed["tp3"],
                                    tp4=parsed["tp4"], tp5=parsed["tp5"],
                                    tp6=parsed.get("tp6"), tp7=parsed.get("tp7"),
                                    tp8=parsed.get("tp8"),
                                    lot_size=lot, tick=tick, strategy=strategy,
                                    tg_source=channel_name,
                                )
                                executed = True
                                exec_lot = lot
                                exec_price = trade_result.get("entry_price")
                            except Exception as _tpl_exc:
                                skip_reason = f"Grid template order failed: {_tpl_exc}"
                                log.warning(
                                    "[%s] grid template immediate placement failed: %s",
                                    source_label, _tpl_exc,
                                )
                            return {
                                "executed": executed, "exec_lot": exec_lot,
                                "exec_price": exec_price, "trade_result": trade_result,
                                "skip_reason": skip_reason, "gap_note": gap_note,
                            }

                        # ── Gap-adjusted market entry (any IME channel) ──────────
                        # When Immediate Market Entry is enabled for this channel,
                        # a signal whose price has already moved past its stated
                        # zone fires immediately at the current market price
                        # instead of queuing to wait for an exact return to zone
                        # -- using the channel's own assigned strategy/template,
                        # with SL/TP/zone shifted by the same distance price has
                        # already moved so the original risk/reward shape is
                        # preserved from the actual fill rather than the now-
                        # stale zone.
                        #
                        # Was scoped to "gold diggers vip"/"gold diggers 2.0" by
                        # channel-name substring match with a hard gap cap
                        # (15pt/10pt) -- generalised to every IME-enabled channel
                        # with no distance cap (2026-08-12, explicit user
                        # direction), after GOLD DIGGERS INSTITUTIONAL queued a
                        # signal only ~2pt outside its zone instead of firing.
                        if not in_range and ime_enabled_for_channel(rs, channel_name):
                            gap = (
                                round(cur_px - eh, 2) if direction == "BUY"
                                else round(el - cur_px, 2)
                            )
                            if gap > 0:
                                sign = 1.0 if direction == "BUY" else -1.0
                                parsed = dict(parsed)
                                parsed["stop_loss"] = round(float(parsed["stop_loss"]) + sign * gap, 2)
                                for tp_i in range(1, 9):
                                    tp_v = parsed.get(f"tp{tp_i}")
                                    if tp_v is not None:
                                        parsed[f"tp{tp_i}"] = round(float(tp_v) + sign * gap, 2)
                                parsed["entry_low"] = round(el + sign * gap, 2)
                                parsed["entry_high"] = round(eh + sign * gap, 2)
                                entry_mid = (parsed["entry_low"] + parsed["entry_high"]) / 2
                                in_range = True
                                gap_note = (
                                    f"Gap-adjusted +{gap:.1f}pt — zone was "
                                    f"{el:.2f}–{eh:.2f}, market at {cur_px:.2f}. "
                                    f"Levels shifted to match fill."
                                )
                                log.info(
                                    "[%s] Gap-adjusted market entry (IME): zone %.2f–%.2f, "
                                    "market %.2f, gap=%.2f pts → SL %.2f  TP1 %.2f",
                                    source_label, el, eh, cur_px, gap,
                                    parsed["stop_loss"], parsed.get("tp1") or 0,
                                )

                        if not in_range:
                            side = "above" if direction == "BUY" else "below"
                            with db_module.db() as conn:
                                conn.execute(
                                    """INSERT INTO vantage_signals
                                       (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                                        tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                                        lot_size,notes,status,created_at)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (signal_id, f"Telegram Auto ({source_label})",
                                     parsed["direction"], parsed["entry_low"], parsed["entry_high"],
                                     parsed["stop_loss"],
                                     parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
                                     parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
                                     lot,
                                     f"Queued: {direction} price ${cur_px:.2f} is {side} zone "
                                     f"${el:.2f}–${eh:.2f}",
                                     "pending", time.time()),
                                )
                                conn.execute(
                                    "UPDATE vantage_tg_signals SET status='pending',signal_id=?"
                                    " WHERE tg_message_id=?",
                                    (signal_id, tg_id),
                                )
                            log.info("[%s] Signal queued (price $%.2f %s zone $%.2f–$%.2f)",
                                    source_label, cur_px, side, el, eh)
                            skip_reason = (
                                f"Signal queued — {direction} price ${cur_px:.2f} is {side} "
                                f"the entry zone ${el:.2f}–${eh:.2f}. "
                                f"Will auto-activate when price returns to zone."
                            )
                        else:
                            with db_module.db() as conn:
                                conn.execute(
                                    """INSERT INTO vantage_signals
                                       (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                                        tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                                        lot_size,notes,status,created_at,activated_at)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (signal_id, f"Telegram Auto ({source_label})",
                                     parsed["direction"], parsed["entry_low"], parsed["entry_high"],
                                     parsed["stop_loss"],
                                     parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
                                     parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
                                     lot, f"Auto-executed from Telegram {tg_id} ({source_label})",
                                     "active", time.time(), time.time()),
                                )
                                conn.execute(
                                    "UPDATE vantage_tg_signals SET status='activated',signal_id=?"
                                    " WHERE tg_message_id=?",
                                    (signal_id, tg_id),
                                )
                            if strategy in (STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER):
                                tg_co_sign = 1.0 if parsed["direction"].upper() == "BUY" else -1.0
                                tg_sl_pt = get_strategy_params(strategy)["sl_pt"]
                                tg_sl_use = round(entry_mid - tg_co_sign * tg_sl_pt, 2)
                            elif strategy == STRATEGY_ADAPTIVE_RUNNER_2:
                                # Fixed SL, not derived from the signal at all -- unlike
                                # Conservative/Scalp Runner, TPs are left as the signal
                                # sent them (see the post-fill block below).
                                tg_co_sign = 1.0 if parsed["direction"].upper() == "BUY" else -1.0
                                _ar2_sl_pt = get_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2)["sl_pt"]
                                tg_sl_use = round(entry_mid - tg_co_sign * _ar2_sl_pt, 2)
                            else:
                                tg_sl_use = float(parsed["stop_loss"])
                            try:
                                trade_result = await open_trade_fn(
                                    signal_id=signal_id, direction=parsed["direction"],
                                    entry_low=float(parsed["entry_low"]),
                                    entry_high=float(parsed["entry_high"]),
                                    stop_loss=tg_sl_use,
                                    tp1=parsed["tp1"], tp2=parsed["tp2"], tp3=parsed["tp3"],
                                    tp4=parsed["tp4"], tp5=parsed["tp5"],
                                    tp6=parsed.get("tp6"), tp7=parsed.get("tp7"),
                                    tp8=parsed.get("tp8"),
                                    lot_size=lot, tick=tick, strategy=strategy,
                                    tg_source=channel_name,
                                )
                                executed = True
                                exec_lot = lot
                                exec_price = trade_result.get("entry_price")
                                if (not trade_result.get("executed_remotely")
                                        and strategy in _SL_OVERRIDE_STRATEGIES
                                        and trade_result.get("trade_id")):
                                    _p = get_strategy_params(strategy)
                                    if strategy == STRATEGY_SCALP_RUNNER:
                                        sl_pt, tp1_pt, tp2_pt = _p["sl_pt"], _p["tp1_pt"], _p["tp2_pt"]
                                    else:
                                        sl_pt, tp1_pt, tp2_pt = _p["sl_pt"], _p["tp1_pt"], None
                                    fill = float(exec_price or entry_mid)
                                    co_sign = 1.0 if parsed["direction"].upper() == "BUY" else -1.0
                                    exact_sl = round(fill - co_sign * sl_pt, 2)
                                    exact_tp1 = round(fill + co_sign * tp1_pt, 2)
                                    exact_tp2 = (
                                        round(fill + co_sign * tp2_pt, 2)
                                        if tp2_pt is not None else None
                                    )
                                    with db_module.db() as conn:
                                        conn.execute(
                                            """UPDATE vantage_simulated_trades
                                               SET stop_loss=?, tp1=?, tp2=?, tp3=NULL,
                                                   tp4=NULL, tp5=NULL, tp6=NULL, tp7=NULL, tp8=NULL
                                               WHERE trade_id=?""",
                                            (exact_sl, exact_tp1, exact_tp2, trade_result["trade_id"]),
                                        )
                                    mt5_tkt = trade_result.get("mt5_ticket")
                                    if mt5_tkt:
                                        try:
                                            await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
                                        except Exception as _e:
                                            log.warning("[%s] TG modify_order SL sync failed: %s", strategy, _e)
                                    trade_result["stop_loss"] = exact_sl
                                    trade_result["tp1"] = exact_tp1
                                    trade_result["tp2"] = exact_tp2
                                    log.info(
                                        "[%s/tg] trade_id=%s fill=%.2f "
                                        "SL=%.2f(-%.1fpt) TP1=%.2f(+%.1fpt)%s",
                                        strategy, trade_result["trade_id"][:8], fill,
                                        exact_sl, sl_pt, exact_tp1, tp1_pt,
                                        f" TP2={exact_tp2:.2f}(+{tp2_pt:.1f}pt)" if exact_tp2 is not None else "",
                                    )
                                    if trade_result.get("managed_by") == "ea":
                                        try:
                                            _ea = ea_bridge.get_instance()
                                            if _ea is not None:
                                                tg_new_tps = {1: exact_tp1}
                                                if exact_tp2 is not None:
                                                    tg_new_tps[2] = exact_tp2
                                                await _ea.update_trade(trade_result["trade_id"], tg_new_tps)
                                        except Exception as _e:
                                            log.warning(
                                                "EA update_trade after %s TG fill failed: %s", strategy, _e
                                            )
                                elif (not trade_result.get("executed_remotely")
                                        and strategy == STRATEGY_ADAPTIVE_RUNNER_2
                                        and trade_result.get("trade_id")):
                                    # SL-only post-fill exact recompute (slippage
                                    # correction) -- TPs are the signal's own, left
                                    # untouched, unlike the Conservative/Scalp Runner
                                    # block above.
                                    _ar2_p = get_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2)
                                    fill = float(exec_price or entry_mid)
                                    co_sign = 1.0 if parsed["direction"].upper() == "BUY" else -1.0
                                    exact_sl = round(fill - co_sign * _ar2_p["sl_pt"], 2)
                                    with db_module.db() as conn:
                                        conn.execute(
                                            "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                                            (exact_sl, trade_result["trade_id"]),
                                        )
                                    mt5_tkt = trade_result.get("mt5_ticket")
                                    if mt5_tkt:
                                        try:
                                            await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
                                        except Exception as _e:
                                            log.warning("[adaptive_runner_2] TG modify_order SL sync failed: %s", _e)
                                    trade_result["stop_loss"] = exact_sl
                                    log.info(
                                        "[adaptive_runner_2/tg] trade_id=%s fill=%.2f SL=%.2f(-%.1fpt fixed)",
                                        trade_result["trade_id"][:8], fill, exact_sl, _ar2_p["sl_pt"],
                                    )
                            except Exception as e:
                                e_str = str(e)
                                is_cb = "circuit breaker" in e_str.lower()
                                is_stood_down = "stood down" in e_str.lower()
                                if is_stood_down:
                                    log.info(
                                        "[%s] Signal deferred to active node (stood down): %s",
                                        source_label, e_str,
                                    )
                                    with db_module.db() as conn:
                                        conn.execute(
                                            "UPDATE vantage_signals SET status='pending', activated_at=NULL"
                                            " WHERE signal_id=?",
                                            (signal_id,),
                                        )
                                    return {"executed": executed, "exec_lot": exec_lot, "exec_price": exec_price,
                                            "trade_result": trade_result, "skip_reason": skip_reason,
                                            "gap_note": gap_note, "deferred_stood_down": True}
                                elif is_cb:
                                    log.warning("[CB] TG trade blocked for %s: %s", source_label, e_str)
                                else:
                                    log.error("[%s] Auto-exec failed: %s", source_label, e)
                                with db_module.db() as conn:
                                    conn.execute(
                                        "UPDATE vantage_signals SET status='pending', activated_at=NULL"
                                        " WHERE signal_id=?",
                                        (signal_id,),
                                    )
                                skip_reason = e_str if is_cb else f"Auto-execution failed: {e_str}"

    return {"executed": executed, "exec_lot": exec_lot, "exec_price": exec_price,
            "trade_result": trade_result, "skip_reason": skip_reason, "gap_note": gap_note}
