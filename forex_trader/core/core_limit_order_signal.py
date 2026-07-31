"""Limit Runner (STRATEGY_LIMIT_RUNNER) placement -- the only strategy that
places a genuine broker-side pending order (BuyLimit/SellLimit via the EA)
instead of waiting for price to re-enter a zone and then filling at market.

Triggered exclusively by parse_limit_order_signal()'s "BUY/SELL [LIMITS]
GOLD @ high/low AREA" message layout (signal_parser.py), which
classify_and_parse() checks for ahead of every channel's own configured
parser_format -- so this fires for any channel, format-matched only, per
the explicit design decision behind this feature (2026-07-22).

Split entry/management (2026-07-28): the *entry* mechanic above is what's
format-triggered -- a genuine resting BuyLimit/SellLimit at the near zone
edge, which no other strategy can express (every other strategy waits for
price to re-enter the zone and then fills at market). The *management* of
the resulting position is a separate question, and a channel that has an
explicit strategy override configured (Trading > Strategy > Channel
Strategy) now keeps that override's TP ladder / BE / trail rules once the
order fills, instead of being silently forced onto Limit Runner's own
ladder. Raised live: GOLD DIGGERS INSTITUTIONAL is set to Signal Climber,
but every "[LIMITS]"-formatted message on it was executing and managing as
Limit Runner, whose payoff profile on that channel is upside-down (avg
loss ~2x avg win over 34 closed trades). Channels with no override are
unchanged -- still pure Limit Runner, since there is no configured intent
to honour and the global Active Strategy is not a per-channel decision.

This mirrors what Reversal Engine's own LIMIT ORDER toggle already does
(reversal_engine_live_execute.py) -- place_pending_order() has always
taken an explicit `strategy` and stored it on vantage_pending_orders, and
_on_pending_order_filled reads it back to stamp the filled trade row, so
the EA manages the fill under that strategy's branch with no extra
protocol work. EA Templates are handled earlier still (engine.py's
dispatch) and never reach here.

Requires the EA bridge to be connected and healthy: unlike every other
strategy's open_trade() handoff, there is no Python-bridge fallback for a
genuine pending order -- if the EA is unavailable, this signal is skipped
with an explanatory skip_reason rather than silently falling back to a
different (misleading) execution model. The message still gets recorded in
vantage_tg_signals either way, same as every other skipped signal.

Entry Realignment (Parsing > Logic Keywords, off by default): if the
market has already moved through the signalled zone by the time this runs,
a resting BuyLimit/SellLimit at the near edge of that zone is no longer a
valid broker price -- root-caused live 2026-07-23 (GOLD DIGGERS
INSTITUTIONAL tg_id=24828, rejected "Invalid price" because gold had
already rallied through the zone a full minute before the signal was even
posted). When enabled, that case enters at market instead and shifts SL/
TPs by the breach delta, preserving the signal's original risk and R:R
distances rather than losing the trade outright.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from forex_trader.core import database as db_module
from forex_trader.core.core_closed_market_queue import queue_closed_market_limit, should_queue
from forex_trader.core.core_strategy_params import get_strategy_params
from forex_trader.core.models import MAX_TP, STRATEGY_LIMIT_RUNNER

log = logging.getLogger(__name__)

# MT5 GTC-with-expiration. Was 240 (4h), copied from the Python-simulated
# zone-wait signals' own pending-expiry convention (core_pending_signal_
# activation.py) -- but that comparison doesn't hold: those signals get
# re-validated (ML gate + momentum/exhaustion check) at the moment they'd
# fire, while a genuine broker-side pending order has no such moment at all
# -- MT5 fills it directly the instant price touches, with no round-trip
# back to Python (see core_pending_order_revalidation.py, which now
# periodically re-checks resting orders against current conditions and
# cancels on invalidation -- the TTL is a backstop for that, not the only
# safety net, but shouldn't be the same 4h a re-validated signal gets away
# with). Matches Reversal Engine's own limit-order toggle and ORB/IVB's
# existing (already-shorter) convention.
_DEFAULT_EXPIRE_MINUTES = 60


def _limit_runner_pcts(n: int, tp_open: bool, params: dict) -> list[float]:
    """Split evenly across `n` numeric TPs. If the signal carried a literal
    "TP OPEN" line, reserve runner_reserve_pct for the permanently-open
    runner leg (the ladder handler's close_full_on_last=False then leaves
    that share open indefinitely once the last numeric TP closes its own
    slice); otherwise split the full 100% (the last TP closes everything
    remaining regardless of its own pcts entry -- see run_tp_ladder)."""
    if tp_open:
        reserve = min(max(float(params.get("runner_reserve_pct", 25.0)) / 100.0, 0.0), 0.95)
        each = (1.0 - reserve) / n
    else:
        each = 1.0 / n
    return [each] * n


def _resolve_management(
    channel_name: str, n: int, tp_open: bool,
) -> tuple[str, list[float], int, str | None]:
    """Decide which strategy manages this position once the resting order
    fills, and with what ladder shape.

    Returns (strategy, pcts, be_at_pos, trail_mode).

    Default is Limit Runner's own even split -- unchanged for any channel
    with no configured override. When the channel HAS an explicit override
    (not 'auto', not an EA Template -- templates never reach this module,
    engine.py dispatches them first), that strategy manages the fill:
      * ladder-shaped strategies (Signal Climber, Reversal Runner, Adaptive
        Runner 1/2) contribute their own pcts/be_at_pos/trail_mode from the
        shared tables in core_open_trade.py, so a tuning change there
        applies here automatically with no MQL5 rebuild;
      * any other strategy gets inert placeholders, exactly as
        reversal_engine_live_execute.py's limit path already does -- its EA
        management branch (ManageConservativeLike etc.) never reads
        t.pcts/t.beAtPos at all.

    A literal "TP OPEN" line still reserves runner_reserve_pct regardless of
    which strategy manages the fill: the reserve is a property of the
    *signal*, not of the ladder, so an override strategy's table is scaled
    down to leave the same permanently-open share Limit Runner would have.
    """
    params = get_strategy_params(STRATEGY_LIMIT_RUNNER)
    default = (
        STRATEGY_LIMIT_RUNNER,
        _limit_runner_pcts(n, tp_open, params),
        max(int(params.get("be_at_pos", 1)) - 1, 0),
        None,
    )

    from forex_trader.core.core_db_channel import get_channel_strategy_override
    try:
        override = get_channel_strategy_override(channel_name)
    except Exception:
        return default
    if not override or override == "auto" or override == STRATEGY_LIMIT_RUNNER:
        return default

    from forex_trader.core.core_ea_templates import is_template_override
    if is_template_override(override):
        # Should be unreachable -- engine.py routes template channels away
        # from this module entirely -- but a template's settings live in a
        # different shape altogether, so never try to ladder one here.
        return default

    from forex_trader.core.core_open_trade import (
        _EA_LADDER_PCTS, _EA_LADDER_BE_AT_POS, _EA_LADDER_TRAIL_MODE,
    )
    if override not in _EA_LADDER_PCTS:
        return override, [1.0], 0, None

    table = _EA_LADDER_PCTS[override]
    pcts = list(table.get(n, table[max(table)]))
    if tp_open:
        reserve = min(max(float(params.get("runner_reserve_pct", 25.0)) / 100.0, 0.0), 0.95)
        pcts = [p * (1.0 - reserve) for p in pcts]
    return (
        override,
        pcts,
        _EA_LADDER_BE_AT_POS[override],
        _EA_LADDER_TRAIL_MODE.get(override),
    )


async def handle_limit_order_signal(
    parsed: dict,
    tg_id: str,
    channel_name: str,
    source_label: str,
    rs: dict,
    sess_ok: bool,
    per_signal_skip: bool,
    per_signal_skip_reason: str,
    skip_reason: str,
    get_trading_balance_fn: Callable[[], Awaitable[float]],
    suggest_lot_size_fn: Callable[[float, float, float, float], float],
    bridge: Any = None,
) -> dict:
    """Places a genuine pending limit order via the EA for a parsed
    "BUY/SELL [LIMITS] GOLD @ high/low AREA" signal. Returns
    {'skip_reason': str} -- there is no 'executed'/'exec_lot'/'exec_price'
    equivalent since nothing has filled yet; the caller's Telegram alert
    reports the placement (or skip) via skip_reason, same as the existing
    "signal queued, will auto-activate" wording used by the zone-wait path
    for every other strategy. (Entry Realignment is the one exception --
    it genuinely fills at market, but still reports purely via skip_reason
    to avoid touching the caller's alert-building logic.)

    `bridge` (the HTTP MT5 bridge, for get_tick()) is only used by Entry
    Realignment's breach check; every other path ignores it. None is a
    valid value -- realignment then simply can't fire.
    """
    if not sess_ok:
        # Queue Closed Market Limits -- only for a genuine weekend close, not
        # for a session the user deliberately switched off (should_queue
        # checks is_weekly_market_closed itself). Queued here rather than at
        # the call site so the market-closed EA-health check below, which
        # would also fail with the terminal down over a weekend, can't reject
        # the signal before it ever reaches the queue.
        if should_queue(rs):
            if queue_closed_market_limit(tg_id, channel_name, source_label, parsed):
                return {"skip_reason": (
                    "Market closed — limit order queued, will be placed automatically "
                    "when the market reopens."
                )}
            return {"skip_reason": "Market closed — limit order already queued."}
        return {"skip_reason": skip_reason}
    if per_signal_skip:
        return {"skip_reason": f"Auto-eval declined signal: {per_signal_skip_reason}"}

    from forex_trader.core import ea_bridge as _ea_mod
    _ea = _ea_mod.get_instance()
    if _ea is None or not _ea.is_ea_healthy():
        return {
            "skip_reason": (
                "Limit order skipped — EA not connected. Pending orders require "
                "a live EA bridge; there is no Python-bridge fallback for this strategy."
            ),
        }

    direction  = parsed["direction"].upper()
    entry_low  = float(parsed["entry_low"])
    entry_high = float(parsed["entry_high"])
    stop_loss  = float(parsed["stop_loss"])
    tps = {n: float(parsed[f"tp{n}"]) for n in range(1, MAX_TP + 1) if parsed.get(f"tp{n}") is not None}
    if not tps:
        return {"skip_reason": "Limit order skipped — signal has no TP levels."}

    n = len(tps)
    tp_open = bool(parsed.get("tp_open"))
    manage_strategy, pcts, be_at_pos, trail_mode = _resolve_management(
        channel_name, n, tp_open,
    )

    # Near edge of the quoted AREA -- the price side reached first as the
    # market approaches the zone from outside it (BUY: top of the zone,
    # SELL: bottom), same boundary price_in_entry_range() already treats as
    # "in zone" for the Python-simulated path (core_scan_messages_auto_execute.py).
    price = entry_high if direction == "BUY" else entry_low

    balance = await get_trading_balance_fn()
    lot = suggest_lot_size_fn(price, stop_loss, balance, float(rs.get("risk_per_trade_pct", 0.5)))
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if strategy_lot > 0:
        lot = strategy_lot

    if bool(rs.get("lk_entry_realignment", 0)) and bridge is not None:
        tick = await bridge.get_tick()
        breached = tick is not None and (
            tick.ask >= price if direction == "BUY" else tick.bid <= price
        )
        if breached:
            return await _open_realigned_market_order(
                _ea, tg_id, channel_name, source_label, direction, lot,
                tick, price, stop_loss, tps, pcts, be_at_pos, tp_open,
                entry_low, entry_high, manage_strategy, trail_mode,
            )

    trade_id = str(uuid.uuid4())[:16]
    try:
        ack = await _ea.place_pending_order(
            trade_id, direction, price, lot, stop_loss, tps, pcts, be_at_pos,
            strategy=manage_strategy,
            expire_minutes=_DEFAULT_EXPIRE_MINUTES,
            close_full_on_last=not tp_open,
            trail_mode=trail_mode,
        )
    except Exception as exc:
        log.warning("[LimitRunner] place_pending_order failed for tg_id=%s: %s", tg_id, exc)
        return {"skip_reason": f"Limit order failed — {exc}"}

    if ack.get("type") != "pending_order_placed":
        return {"skip_reason": f"Limit order rejected by EA — {ack.get('error', 'unknown error')}"}

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    manage_name = _strategy_label(manage_strategy)
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Auto ({source_label})", direction, entry_low, entry_high,
             stop_loss, tps.get(1), tps.get(2), tps.get(3), tps.get(4), tps.get(5),
             tps.get(6), tps.get(7), tps.get(8), lot,
             f"Limit order pending @ {price:.2f}, managed as {manage_name} "
             f"(EA ticket {ticket})",
             "pending", now),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='pending',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_id),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, tg_id, channel_name, direction, price, stop_loss,
             json.dumps(tps), json.dumps(pcts), be_at_pos, int(tp_open), lot, ticket,
             "working", now, manage_strategy),
        )
    log.info(
        "[LimitRunner] pending order placed tg_id=%s ticket=%s %s %.2f lots @ %.2f "
        "SL=%.2f tps=%s tp_open=%s manage=%s",
        tg_id, ticket, direction, lot, price, stop_loss, tps, tp_open, manage_strategy,
    )
    return {
        "skip_reason": (
            f"Limit order placed (managed as {manage_name}) — {direction} {lot:g} lots "
            f"@ {price:.2f} (EA ticket {ticket}), SL {stop_loss:.2f}. Will notify on fill."
        ),
        "manage_strategy": manage_strategy,
    }


def _strategy_label(strategy: str) -> str:
    from forex_trader.core.models import STRATEGY_NAMES
    return STRATEGY_NAMES.get(strategy, strategy)


async def _open_realigned_market_order(
    ea: Any, tg_id: str, channel_name: str, source_label: str, direction: str,
    lot: float, tick: Any, original_price: float, stop_loss: float,
    tps: dict[int, float], pcts: list[float], be_at_pos: int, tp_open: bool,
    entry_low: float, entry_high: float,
    manage_strategy: str = STRATEGY_LIMIT_RUNNER, trail_mode: str | None = None,
) -> dict:
    """Entry Realignment fallback -- opens a genuine immediate market order
    via the EA (the same low-level call every ladder-shaped strategy's EA
    handoff uses, see core_open_trade.py) with SL/TPs shifted by the breach
    delta, instead of attempting a resting order the broker would reject
    outright. Writes directly to vantage_simulated_trades (order_type=
    'market', managed_by='ea') -- there is no vantage_pending_orders row
    since nothing was ever left resting.
    """
    market_px = tick.ask if direction == "BUY" else tick.bid
    delta = market_px - original_price
    realigned_sl  = round(stop_loss + delta, 2)
    realigned_tps = {n: round(v + delta, 2) for n, v in tps.items()}

    trade_id = str(uuid.uuid4())[:16]
    try:
        ack = await ea.open_trade(
            trade_id, direction, lot, realigned_sl, realigned_tps,
            strategy=manage_strategy, pcts=pcts, be_at_pos=be_at_pos,
            trail_mode=trail_mode,
        )
    except Exception as exc:
        log.warning("[LimitRunner] realigned open_trade failed for tg_id=%s: %s", tg_id, exc)
        return {"skip_reason": f"Entry realignment failed — {exc}"}

    if ack.get("type") != "trade_opened":
        return {"skip_reason": f"Entry realignment rejected by EA — {ack.get('error', 'unknown error')}"}

    ticket = ack.get("ticket")
    fill_price = float(ack.get("fill_price", market_px))
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Auto ({source_label})", direction, entry_low, entry_high,
             realigned_sl, realigned_tps.get(1), realigned_tps.get(2), realigned_tps.get(3),
             realigned_tps.get(4), realigned_tps.get(5), realigned_tps.get(6),
             realigned_tps.get(7), realigned_tps.get(8), lot,
             f"Limit order entry realigned — zone already breached, entered at "
             f"market {fill_price:.2f} (was {original_price:.2f}), managed as "
             f"{_strategy_label(manage_strategy)}, EA ticket {ticket}",
             "active", now),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='active',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_id),
        )
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,
                tg_source,managed_by,tp_open,order_type,pending_placed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, ticket, direction, entry_low, entry_high, fill_price,
             lot, lot, realigned_sl,
             realigned_tps.get(1), realigned_tps.get(2), realigned_tps.get(3),
             realigned_tps.get(4), realigned_tps.get(5), realigned_tps.get(6),
             realigned_tps.get(7), realigned_tps.get(8),
             "open", now, 0.0, 0.0, 0.0, 0.0, manage_strategy,
             channel_name, "ea", int(tp_open), "market", None),
        )
    log.info(
        "[LimitRunner] entry realigned tg_id=%s ticket=%s %s %.2f lots @ %.2f "
        "(was %.2f, delta %+.2f) SL=%.2f tps=%s manage=%s",
        tg_id, ticket, direction, lot, fill_price, original_price, delta,
        realigned_sl, realigned_tps, manage_strategy,
    )
    return {
        "skip_reason": (
            f"Entry realigned (managed as {_strategy_label(manage_strategy)}) — zone "
            f"already breached, entered at market {fill_price:.2f} lots {lot:g} "
            f"(was {original_price:.2f}), SL {realigned_sl:.2f} (EA ticket {ticket})."
        ),
        "manage_strategy": manage_strategy,
    }
