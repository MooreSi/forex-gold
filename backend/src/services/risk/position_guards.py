"""Two pre-trade refusals on Settings > Risk, both OFF by default (docs/todo/026).

Owner, 2026-10-22, after three Reversal Engine BUYs lost $59.68 between 00:10
and 01:00 UTC:

* **Never add to a loser** (`never_add_to_loser_enabled`). Refuse an order
  while an open trade in the same direction, from any source, is losing at
  the current price. Over 808 real Reversal Engine orders (2026-07-07 to
  10-22), the 72 added to a same-direction trade already underwater averaged
  -$12.98, against -$3.76 for an order opened with nothing open that way.
* **Count open risk toward the daily limit** (`daily_limit_counts_open_risk`).
  `governor.check_daily_loss_limit` reads closed P&L only, so on 10-22 it
  tripped after the second loss with the third already open, and the day
  closed at 6.3% against a 4% limit. This refuses an order when today's
  closed P&L, minus what every open trade loses at its stop, minus what the
  new trade risks, would pass that same limit. Like that halt, it does not
  need the Risk Governor.

`open_trade` calls `check` once a live price is known, after the circuit
breaker and max-open-trades checks, so every market order passes it and a
refusal never reaches the broker. It reads; it never opens, closes or moves
anything.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.src.services.risk import governor
from backend.src.services.risk import repo as risk_repo
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)


async def check(bridge: Any, *, signal_id: str, direction: str, stop_loss: float,
                lot_size: float, strategy: str, tick: Any, rs: dict) -> None:
    """Raise ValueError naming the rule when the order must not be placed."""
    add_on = bool(rs.get("never_add_to_loser_enabled", 0))
    risk_on = bool(rs.get("daily_limit_counts_open_risk", 0))
    if not (add_on or risk_on):
        return
    positions = [p for p in risk_repo.open_positions_for_guards()
                 if p["signal_id"] != signal_id]
    if add_on:
        _refuse(losing_same_direction(direction, tick, positions))
    if risk_on:
        _refuse(await _open_risk_reason(bridge, direction, stop_loss, lot_size,
                                        strategy, tick, rs, positions))


def _refuse(reason: Optional[str]) -> None:
    if reason:
        log.warning("[RiskGuard] %s", reason)
        raise ValueError(reason)


def losing_same_direction(direction: str, tick: Any, positions: list[dict]) -> Optional[str]:
    """Why a same-direction open trade forbids this order, or None.

    A BUY is losing when its entry is above the current ask: it could be
    bought cheaper now. A SELL mirrors that with the bid. Comparing against
    the price the trade was opened at, not the one it closes at, keeps the
    spread out of it, so a trade that opened a moment ago is not a loser.
    A row with no fill price yet is not counted.
    """
    buy = direction.upper() == "BUY"
    for p in positions:
        entry = p["entry_price"]
        if not entry or str(p["direction"]).upper() != direction.upper():
            continue
        if (buy and tick.ask < entry) or (not buy and tick.bid > entry):
            now = tick.ask if buy else tick.bid
            return (f"Never add to a loser: an open {direction.upper()} from "
                    f"{entry:.2f} is losing (price now {now:.2f}). Order refused.")
    return None


def position_risk(p: dict) -> float:
    """Dollars this open trade loses if its current stop is hit, never below 0.

    While the stop has not moved, a larger recorded `initial_risk` wins: a
    grid row's lots describe one leg and `initial_risk` covers them all. A row
    with no fill price yet has only `initial_risk`.
    """
    recorded = float(p.get("initial_risk") or 0.0)
    entry, sl = p.get("entry_price"), p.get("stop_loss")
    if not entry or not sl:
        return recorded
    lots = float(p.get("remaining_lots") or p.get("lot_size") or 0.0)
    sign = 1.0 if str(p["direction"]).upper() == "BUY" else -1.0
    at_stop = max(0.0, sign * (entry - sl) * lots * CONTRACT_SIZE)
    if p.get("initial_sl") is not None and abs(float(p["initial_sl"]) - sl) < 1e-9:
        return max(at_stop, recorded)
    return at_stop


def new_trade_risk(direction: str, stop_loss: float, lot_size: float,
                   strategy: str, tick: Any) -> float:
    """Dollars the order about to be placed risks at its stop, the way
    `open_trade` records `initial_risk`, times the legs of a grid template."""
    ref = tick.ask if direction.upper() == "BUY" else tick.bid
    legs = 1
    from backend.src.services.broker import ea_templates
    if ea_templates.is_template_override(strategy):
        tpl = ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))
        if tpl and tpl.get("mode") == "grid":
            legs = max(1, int(tpl.get("anchors", 0) or 0) + int(tpl.get("pendings", 0) or 0))
    return abs(ref - stop_loss) * lot_size * CONTRACT_SIZE * legs


async def _open_risk_reason(bridge, direction, stop_loss, lot_size, strategy, tick,
                            rs, positions) -> Optional[str]:
    """`governor.check_daily_loss_limit`'s limit, measured the same way (its
    day, its resume baseline, its balance), with what is open and what is
    about to open counted as already lost."""
    max_daily = float(rs.get("max_daily_loss_pct", 0) or 0)
    if max_daily <= 0:
        return None
    balance = await _live_balance(bridge)
    if balance is None:
        log.warning("[RiskGuard] open risk not counted: no live balance from the bridge")
        return None
    realised = governor.daily_loss_realised()
    day_base = balance - realised
    if day_base <= 0:
        return None
    limit = day_base * (max_daily / 100.0)
    open_risk = sum(position_risk(p) for p in positions)
    new_risk = new_trade_risk(direction, stop_loss, lot_size, strategy, tick)
    worst = realised - open_risk - new_risk
    if worst > -abs(limit):
        return None
    return (f"Open risk counts toward the daily loss limit: ${realised:.2f} closed today, "
            f"${open_risk:.2f} at risk in open trades and ${new_risk:.2f} on this order "
            f"would reach ${worst:.2f} against -${abs(limit):.2f} "
            f"({max_daily:.1f}% of ${day_base:,.2f}). Order refused.")


async def _live_balance(bridge: Any) -> Optional[float]:
    try:
        acc = await bridge.get_account()
        balance = float((acc or {}).get("balance") or 0)
    except Exception as exc:
        log.warning("[RiskGuard] account read failed: %s", exc)
        return None
    return balance if balance > 0 else None
