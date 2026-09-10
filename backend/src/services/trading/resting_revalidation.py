"""Re-check resting orders against the market they are about to enter, and
withdraw the ones it has turned against.

reversal-engine/050, widened by limit-orders/040 (owner, 2026-09-10).

A limit order is accepted by the broker long before it fills, and MT5 fills it
directly -- there is no round trip back to Python at the moment of the fill. So
every entry gate that could not be evaluated at placement time is never
evaluated at all, unless something asks again while the order rests. This is
that something.

**It used to ask one question.** The higher-timeframe bias, and only with the
trend gate switched on. Meanwhile a *queued* Telegram signal -- the same setup,
waiting in Python rather than at the broker -- has been re-checked against the
trading schedule, the news blackout, the fill delay, the pre-trade filters and
the last M5 candle since reversal-engine/100, which said so in its own "Still
open": "Only the bias is re-checked on a resting order. Schedule and news are
not." One setup, two answers, decided by where it happened to wait.

**Every gate is called through its own module**, never copied and never bound
with `from x import f` at import time. A second implementation of "are we in a
blackout" is how two routes come to disagree -- the shape behind bugs/024,
reversal-engine/080 and bugs/034 -- and the tests patch at the definition site
precisely to keep that true.

**Proximity.** The bias is one comparison against a value the caller already
holds, so it runs on every sweep at any distance. The rest costs real work and
is asked only once price is within PROXIMITY_PTS of the resting price: far
enough ahead of the fill to act on, close enough that a condition which would
have cleared by fill time does not pull an order an hour early.

**Withdraw and re-arm, not cancel.** A failed gate pulls the broker order but
does not kill the setup: the row goes to 'withdrawn' and is put back if every
gate passes again before the order's ORIGINAL expiry. A news window or a
schedule edge is temporary; the trade the channel sent is not. Three things
that must stay true, each pinned by a test:

  * the re-placed order expires when the original would have, computed from
    `created_at` -- `vantage_pending_orders` has no expiry column. Re-placing
    with a fresh life would make a flapping gate keep an order alive for hours.
  * it is re-placed at the stored price, stop, targets, lot and strategy. If
    the levels would have to move, it is a different trade: leave it withdrawn.
  * exactly one live broker order per setup at every moment. 'withdrawn' is a
    distinct status from 'cancelled' for that reason -- see
    broker/repo.mark_pending_order_withdrawn.

Re-arming is NOT proximity-gated, deliberately: an order that should be on the
book belongs on the book, whatever the distance. Proximity governs pulling one
off, which is the destructive direction.

**It cancels; it never closes.** A resting order has no position, so the worst
this can do is withdraw an order that never filled. Nothing here may reach a
close, and `TestItNeverCloses` asserts that by name against this module's own
source.

**Same fail-open behaviour as before**: a neutral or unreadable bias withdraws
nothing, and a gate that throws withdraws nothing. A sweep that pulled every
resting order because a price feed hiccuped would be a self-inflicted outage,
which is worse than the stale fills it exists to prevent.

The direct evidence for staleness is thin -- signals whose bias changed while
waiting are 20 trades at -$12.08 each against -$2.84 for the 737 where it held,
the right direction but far too small a sample. What justifies this is the
entry gate's own evidence: 201 counter-bias trades at -$1,210.98 across the
whole record.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from backend.src.services.risk import governor as _gov
from backend.src.services.risk import schedule as _schedule
from backend.src.utils import news_calendar as _news

log = logging.getLogger(__name__)

# How near price must come before the expensive gates are asked. Owner,
# 2026-09-10: roughly a minute or two of gold movement in normal conditions.
PROXIMITY_PTS = 10.0


def _tps_of(row: dict) -> dict:
    try:
        return {int(k): float(v) for k, v in json.loads(row.get("tps_json") or "{}").items()}
    except Exception:
        return {}


def _pcts_of(row: dict) -> list:
    try:
        return [float(p) for p in json.loads(row.get("pcts_json") or "[]")]
    except Exception:
        return []


def _distance_to(row: dict, tick: Any) -> Optional[float]:
    """How far price still has to travel to reach this order, or None.

    Signed by approach direction, not absolute: a BUY limit rests BELOW the
    market and is reached by price falling, a SELL limit rests above and is
    reached by price rising. A plain abs() would treat an order price has
    already passed as though it were still an hour away.
    """
    if tick is None:
        return None
    price = float(row.get("price") or 0)
    if price <= 0:
        return None
    if str(row.get("direction") or "").upper() == "BUY":
        return float(tick.ask) - price
    return price - float(tick.bid)


def _momentum_refusal(row: dict, dpm_candles: Any) -> Optional[str]:
    """The queued path's own check: the last completed M5 candle must agree
    with the trade's direction, or the move into the zone looks like a
    fakeout. Same rule as pending_activation, stated once here because that
    one reads a signal row and this reads an order row."""
    if not dpm_candles:
        return None
    last = dpm_candles[-1]
    o = float(last.get("open", 0) or 0)
    c = float(last.get("close", 0) or 0)
    if not o or not c:
        return None
    bullish = c > o
    up = str(row.get("direction") or "").upper() == "BUY"
    if up and not bullish:
        return "last M5 candle is bearish against a BUY (momentum mismatch)"
    if not up and bullish:
        return "last M5 candle is bullish against a SELL (momentum mismatch)"
    return None


def _full_gate_refusal(row: dict, rs: dict, tick: Any, dpm_candles: Any) -> Optional[str]:
    """The first gate that refuses this order, or None if every one passes.

    The same functions the queued path calls (pending_activation.py:432-525),
    through their own modules so a test can patch them where they are defined.

    The pre-trade filters are NOT bypassed for a template here, unlike every
    other route. The reason those routes exempt templates is that a template
    replaces the signal's own SL/TPs, so scoring the signal's numbers judges a
    trade on levels it will never use -- but by the time an order is resting,
    the row already carries the template's own resolved levels
    (limit-orders/020). Scoring them is scoring what the trade will actually
    run on.
    """
    ok, why = _schedule.check_trading_schedule(source=row.get("channel_name") or "")
    if not ok:
        return why or "outside the trading schedule"

    ok, why = _news.check_news_blackout()
    if not ok:
        return why or "news blackout"

    soon = _gov.fill_too_soon(row.get("created_at"), time.time(), rs)
    if soon:
        return soon

    price = float(row.get("price") or 0)
    tps = _tps_of(row)
    filt = _gov.check_pre_trade_filters(
        row.get("direction"), price, price,
        float(row.get("stop_loss") or 0), tps.get(1),
        actual_price=price, source_name=row.get("channel_name") or "",
    )
    if filt:
        return filt

    return _momentum_refusal(row, dpm_candles)


def _refusal_for(row: dict, rs: dict, bias: Optional[str], tick: Any,
                 dpm_candles: Any, ignore_proximity: bool = False) -> Optional[str]:
    """Why this resting order should be off the book, or None.

    `ignore_proximity` is set when asking on behalf of a re-arm. Proximity
    exists to avoid pulling an order early over a condition that may clear
    before price ever arrives; putting one BACK has no such hazard, and an
    order that should be on the book belongs there whatever the distance.
    """
    if bool(rs.get("htf_bias_gate_enabled", 0)):
        bias_why = _gov.htf_bias_blocks(row.get("direction"), bias, rs)
        if bias_why:
            return bias_why
    if not bool(rs.get("resting_revalidation_enabled", 1)):
        return None
    if not ignore_proximity:
        dist = _distance_to(row, tick)
        if dist is None or dist > PROXIMITY_PTS:
            return None
    return _full_gate_refusal(row, rs, tick, dpm_candles)


def _minutes_left(row: dict) -> float:
    """Of the life the order was originally placed with. Derived from
    `created_at`, because the table records no expiry of its own."""
    from backend.src.services.trading.limit_order_signal import _DEFAULT_EXPIRE_MINUTES
    created = float(row.get("created_at") or 0)
    return _DEFAULT_EXPIRE_MINUTES - (time.time() - created) / 60.0


async def _mark(fn, *args) -> None:
    """Write a resting order's new status, never raising.

    Every caller here has ALREADY changed the broker's book by the time it
    gets to this, so a failed write must not unwind or hide that. Logged at
    error rather than debug: the row and the book disagreeing is a real
    divergence, not a quiet retry.
    """
    from backend.src.db import database as _db
    try:
        await _db.to_db_thread(fn, *args)
    except Exception as exc:
        log.error("[Resting] broker order changed but its row did not (%s): %s",
                  args[0] if args else "?", exc)


async def _withdraw(ea, repo, row: dict, reason: str) -> bool:
    ticket = int(row.get("ea_ticket") or 0)
    if ticket <= 0:
        # No broker ticket recorded: there is nothing to withdraw, and
        # guessing one would cancel someone else's order.
        return False
    if not await ea.cancel_pending_order(row["trade_id"], ticket, reason):
        return False
    # From here the broker order is GONE, whatever happens next. The status
    # write is therefore isolated: letting it raise into the caller's per-row
    # handler would lose the fact of a cancellation that has already happened
    # -- the sweep would under-report, and nothing would say the row and the
    # book now disagree. Found by tests/trading/test_resting_orders_are_
    # revalidated.py, which runs the sweep with no database at all.
    #
    # The degraded state is safe but must be loud: the row still reads
    # 'working', so the next sweep tries to cancel a ticket that no longer
    # exists (the EA refuses, harmlessly) and the order can never be re-armed.
    await _mark(repo.mark_pending_order_withdrawn, row["trade_id"], reason, time.time())
    # No Telegram message yet: that is limit-orders/050, which owns the
    # formatters and the flap damping. Deliberately not stubbed here -- a
    # function that is called and does nothing is the shape this repo's
    # CLAUDE.md warns about, and a silent one would be indistinguishable from
    # a working one. Until 050 lands, a withdrawal is visible in the log only.
    log.info("[Resting] withdrew %s ticket=%s — %s", row["trade_id"], ticket, reason)
    return True


async def _rearm(ea, repo, row: dict, rs: dict, bias: Optional[str], tick: Any,
                 dpm_candles: Any) -> bool:
    # The same gate set that withdrew it decides whether it comes back. A
    # condition that would pull the order off the book is not one to put it
    # back onto the book under.
    still_refused = _refusal_for(row, rs, bias, tick, dpm_candles,
                                 ignore_proximity=True)
    if still_refused:
        log.debug("[Resting] %s stays withdrawn — %s", row["trade_id"], still_refused)
        return False
    left = _minutes_left(row)
    if left <= 0:
        await _mark(repo.mark_pending_order_expired, row["trade_id"], time.time())
        log.info("[Resting] %s expired while withdrawn", row["trade_id"])
        return False

    template = None
    strategy = row.get("strategy") or ""
    from backend.src.services.broker import ea_templates as _tpl_mod
    if _tpl_mod.is_template_override(strategy):
        template = _tpl_mod.get_ea_template(_tpl_mod.template_name_from_override(strategy))

    ack = await ea.place_pending_order(
        row["trade_id"], str(row.get("direction") or "").upper(),
        float(row["price"]), float(row["lot_size"]), float(row["stop_loss"]),
        _tps_of(row), _pcts_of(row), int(row.get("be_at_pos") or 0),
        strategy=strategy,
        expire_minutes=left,
        close_full_on_last=not bool(row.get("tp_open")),
        template=template,
    )
    if ack.get("type") != "pending_order_placed":
        # The broker can refuse -- a limit price the market has since crossed
        # is "Invalid price". Leaving the row withdrawn is correct: recording
        # it as working would strand a row with no order behind it.
        log.info("[Resting] could not re-arm %s — %s",
                 row["trade_id"], ack.get("error", "unknown error"))
        return False
    await _mark(repo.mark_pending_order_rearmed, row["trade_id"],
                ack.get("ticket"), time.time())
    log.info("[Resting] re-armed %s ticket=%s with %.1f minutes left",
             row["trade_id"], ack.get("ticket"), left)
    return True


async def revalidate_resting_orders(
    ea,
    rs: dict,
    bias: Optional[str],
    fetch: Optional[Callable[[], list]] = None,
    tick: Any = None,
    dpm_candles: Any = None,
) -> int:
    """Withdraw every resting order the market now refuses, and put back the
    ones it no longer does.

    Returns how many were withdrawn. `bias` is passed in rather than fetched so
    the caller decides how fresh it needs to be -- the loop that runs this
    already holds one, and a second lookup per sweep would be waste. `tick` and
    `dpm_candles` come from the same cycle for the same reason.

    Never raises: this runs on a loop, and a bad sweep must not take the cycle
    down with it.
    """
    if not (bool(rs.get("htf_bias_gate_enabled", 0))
            or bool(rs.get("resting_revalidation_enabled", 1))):
        return 0
    from backend.src.services.broker import repo as _broker_repo
    try:
        if fetch is None:
            from backend.src.db import database as _db
            rows = await _db.to_db_thread(_broker_repo.fetch_revalidatable_pending_orders)
        else:
            rows = fetch()
    except Exception as exc:
        log.debug("[Resting] could not read resting orders: %s", exc)
        return 0

    withdrawn = 0
    for row in rows or []:
        try:
            status = str(row.get("status") or "working")
            if status == "withdrawn":
                await _rearm(ea, _broker_repo, row, rs, bias, tick, dpm_candles)
                continue
            reason = _refusal_for(row, rs, bias, tick, dpm_candles)
            if not reason:
                continue
            if await _withdraw(ea, _broker_repo, row, reason):
                withdrawn += 1
        except Exception as exc:
            # One order the EA refuses -- filled in the meantime is the obvious
            # case -- must not abandon the rest of the sweep.
            log.warning("[Resting] could not revalidate %s: %s", row.get("trade_id"), exc)
    return withdrawn
