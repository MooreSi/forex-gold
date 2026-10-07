"""Risk > Daily goal > "Move stops to breakeven once the goal is reached".

Owner, 2026-09-30. Reaching the goal with trades still open stops new entries
(`risk/daily_goal.py`), but the open trades can still lose and take the day
back under the goal. With this tickbox on, each open trade's stop is moved to
breakeven so they cannot.

Decided with the owner the same day:

- **Breakeven is entry plus costs**, the same round-trip estimate the TP
  safety net uses (`safety_net.compute_be_cost_pts`: spread, slippage,
  commission), so a stop-out nets about $0 rather than minus the spread.
  Swap is not in it.
- **A trade not yet far enough in profit is moved later, not closed.** Every
  sweep while the goal stands looks again, so a trade in loss is protected
  as soon as price gives it room.

Rules the code keeps:

- **Only ever tightens.** A stop already at or beyond breakeven is left alone.
- **Waits for `MIN_GAP` of room beyond breakeven.** The bridge clamps a stop
  closer to price than the broker's stops level (`mt5_bridge._modify_position`),
  and a clamped stop can land looser than the one already there.
- **Every position on the account**, manual ones included: the goal counts
  every close.
- **The active trader node only.** Both nodes read one MT5 account.
- **The local row is written only after the broker accepted**
  (`panel_repo.record_stop_loss`'s rule), and a rejected move is retried after
  `RETRY_S`, not every sweep.
- **EA-managed trades are modified at the broker directly.** The EA re-reads
  the live stop every tick and its own moves only ever tighten (`MoveSl`), so
  it does not move this one back.

Calls `bridge.modify_order`, a real MT5 order-modify call. This module modifies
no order itself; it only calls whatever `bridge` its caller supplies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.src.db import database as db_module
from backend.src.services.cluster import node_roles
from backend.src.services.positions import repo as positions_repo
from backend.src.services.positions.safety_net import compute_be_cost_pts
from backend.src.services.risk import daily_goal as _goal

log = logging.getLogger(__name__)

__all__ = ["sweep", "SweepState"]

SWEEP_EVERY_S = 5.0
# Price distance (dollars, 100 points) price must be beyond breakeven before
# the stop is sent. Above XAUUSD's usual stops level, so the bridge's clamp
# does not come into it.
MIN_GAP = 1.0
# A rejected move is tried again after this, not every sweep.
RETRY_S = 60.0


@dataclass
class SweepState:
    last_check: float = float("-inf")
    # ticket -> when the broker last rejected a move.
    rejected_at: dict = field(default_factory=dict)


def _be_price(kind: str, entry: float, cost: float) -> float:
    return round(entry + cost, 2) if kind == "BUY" else round(entry - cost, 2)


def _needs_move(kind: str, sl: float, price: float, be: float) -> bool:
    if kind == "BUY":
        return sl < be - 0.005 and price - be >= MIN_GAP
    return (sl <= 0 or sl > be + 0.005) and be - price >= MIN_GAP


async def _protect(state: SweepState, bridge: Any, open_trades: list, now: float) -> None:
    positions = await bridge.get_positions()
    if not positions:
        return
    cost = compute_be_cost_pts({})
    trade_ids = {str(t.get("mt5_ticket")): t.get("trade_id")
                 for t in open_trades if t.get("mt5_ticket")}
    for pos in positions:
        ticket = int(pos.get("ticket") or 0)
        kind = str(pos.get("type") or "").upper()
        entry = float(pos.get("open_price") or 0)
        if not ticket or kind not in ("BUY", "SELL") or not entry:
            continue
        be = _be_price(kind, entry, cost)
        if not _needs_move(kind, float(pos.get("sl") or 0),
                           float(pos.get("current_price") or 0), be):
            continue
        if now - state.rejected_at.get(ticket, float("-inf")) < RETRY_S:
            continue
        try:
            res = await bridge.modify_order(ticket, sl=be, tp=None)
        except Exception as e:
            res = {"error": str(e)}
        if not (res or {}).get("success"):
            state.rejected_at[ticket] = now
            log.warning("[DailyGoal] breakeven move on %s to %.2f rejected: %s",
                        ticket, be, (res or {}).get("error", res))
            continue
        state.rejected_at.pop(ticket, None)
        trade_id = trade_ids.get(str(ticket))
        if trade_id:
            await db_module.to_db_thread(positions_repo.set_stop_loss_be, trade_id, be)
        log.warning("[DailyGoal] goal reached — %s %s stop moved to breakeven+cost %.2f",
                    kind, ticket, be)


async def sweep(state: SweepState, bridge: Any, rs: dict,
                get_open_trades: Callable[[], list],
                now: Optional[float] = None) -> None:
    """One monitor-cycle evaluation, throttled. Never raises."""
    now = time.time() if now is None else now
    if now - state.last_check < SWEEP_EVERY_S:
        return
    state.last_check = now
    try:
        if not bool(int(rs.get("daily_goal_protect_be", 0) or 0)):
            return
        if not bool(int(rs.get("daily_goal_enabled", 0) or 0)):
            return
        if not node_roles.is_active_trader_node():
            return
        # MT5's figure, as the halt uses (owner, 2026-10-07). No answer, no move.
        has_deals, realised, day_realised = await _goal.broker_realised(bridge)
        if has_deals and realised is None:
            return
        balance = None
        if str(rs.get("daily_goal_mode") or "pct") != "usd":
            acc = await bridge.get_account()
            balance = float((acc or {}).get("balance") or 0) or None
        if not await db_module.to_db_thread(_goal.goal_standing, rs, balance,
                                            realised, day_realised):
            return
        open_trades = await db_module.to_db_thread(get_open_trades)
        await _protect(state, bridge, open_trades or [], now)
    except Exception as e:
        log.debug("[DailyGoal] breakeven sweep failed: %s", e)
