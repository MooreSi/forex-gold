"""Re-check resting orders against the trend, and withdraw the ones it turned against.

reversal-engine/050. The higher-timeframe bias gate (080) is evaluated once,
when an order is placed. A limit order can then rest for the better part of an
hour and fill into a bias that has since reversed -- at which point it is a
counter-bias trade the gate would have refused had it been asked. This asks it
again, on the same `governor.htf_bias_blocks`, rather than growing a second
rule that could disagree with the first.

**It cancels; it never closes.** A resting order has no position, so the worst
this can do is withdraw an order that never filled. Nothing here may reach a
close, and a test asserts that by name.

**Same toggle as the entry gate**, and the same fail-open behaviour: a neutral
or unreadable bias withdraws nothing. A sweep that pulled every resting order
because a price feed hiccuped would be a self-inflicted outage, which is worse
than the stale fills it exists to prevent.

The direct evidence for staleness is thin -- signals whose bias changed while
waiting are 20 trades at -$12.08 each against -$2.84 for the 737 where it held,
the right direction but far too small a sample. What justifies this is the
entry gate's own evidence: 201 counter-bias trades at -$1,210.98 across the
whole record.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from backend.src.services.risk import governor as _gov

log = logging.getLogger(__name__)


async def revalidate_resting_orders(
    ea,
    rs: dict,
    bias: Optional[str],
    fetch: Optional[Callable[[], list]] = None,
) -> int:
    """Withdraw every working pending order the current bias now refuses.

    Returns how many were cancelled. `bias` is passed in rather than fetched
    so the caller decides how fresh it needs to be -- the loop that runs this
    already holds one, and a second lookup per sweep would be waste.

    Never raises: this runs on a loop, and a bad sweep must not take the cycle
    down with it.
    """
    if not bool(rs.get("htf_bias_gate_enabled", 0)):
        return 0
    try:
        if fetch is None:
            from backend.src.services.broker import repo as _broker_repo
            from backend.src.db import database as _db
            rows = await _db.to_db_thread(_broker_repo.fetch_working_pending_orders)
        else:
            rows = fetch()
    except Exception as exc:
        log.debug("[Resting] could not read working pending orders: %s", exc)
        return 0

    cancelled = 0
    for row in rows or []:
        try:
            reason = _gov.htf_bias_blocks(row.get("direction"), bias, rs)
            if not reason:
                continue
            ticket = int(row.get("ea_ticket") or 0)
            if ticket <= 0:
                # No broker ticket recorded: there is nothing to withdraw, and
                # guessing one would cancel someone else's order.
                continue
            if await ea.cancel_pending_order(
                    row["trade_id"], ticket, f"bias turned: {reason}"):
                cancelled += 1
                log.info(
                    "[Resting] withdrew %s ticket=%s — %s",
                    row.get("trade_id"), ticket, reason,
                )
        except Exception as exc:
            # One order the EA refuses -- filled in the meantime is the obvious
            # case -- must not abandon the rest of the sweep.
            log.warning("[Resting] could not withdraw %s: %s", row.get("trade_id"), exc)
    return cancelled
