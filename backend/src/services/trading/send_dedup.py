"""Whether the Python-bridge fallback may place an order (stage3/010).

Split out of `open_trade.py` to keep that file inside its size budget; the
decision logic is here, the one call site is there.

The window this guards: `open_trade` hands off to the EA, the EA is merely
SLOW, the ack times out, and the fallback below places a SECOND order for a
trade already on the book. See broker/dedup.py for the broker-side lookup and
why "unknown" is a third state rather than a synonym for "absent".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class _FallbackDecision:
    """Whether the Python-bridge fallback may send, and what to adopt if not."""
    send: bool
    unknown: bool = False
    ticket: Optional[int] = None
    entry_price: float = 0.0
    by_ea: bool = False
    detail: str = ""


def _bridge_order_comment(trade_id: str, signal_id: str) -> str:
    """The comment a Python-bridge order carries.

    Was f"sig:{signal_id[:8]}" -- the SIGNAL id, while the EA stamps the TRADE
    id, so the two send paths could never be correlated and no dedup check was
    possible. Now the trade id, under its own prefix so a bridge order is
    never read as a template leg (see broker/dedup.py).
    """
    from backend.src.services.broker.dedup import comment_for_bridge_order
    return comment_for_bridge_order(trade_id)


async def _resolve_fallback_send(bridge, trade_id: str,
                                 ea_attempted: bool) -> _FallbackDecision:
    """Decide whether the Python-bridge fallback is allowed to place an order.

    Only consulted when the EA was actually asked and did not confirm: that is
    the one window in which an order may already be on the book without this
    process knowing. When the EA was never in the picture there is nothing to
    duplicate, and the ordinary path must not pay for a broker round trip.
    """
    if not ea_attempted:
        return _FallbackDecision(send=True, detail="no EA order was attempted")

    from backend.src.services.broker import dedup as _dedup
    res = await _dedup.find_trade(bridge, trade_id)

    if res.found:
        log.error(
            "[dedup] REFUSING to re-send %s — the broker already has it (%s). "
            "Adopting ticket %s instead. The EA ack was slow, not lost.",
            trade_id[:8], res.detail, res.ticket,
        )
        return _FallbackDecision(send=False, ticket=res.ticket,
                                 entry_price=res.entry_price, by_ea=res.by_ea,
                                 detail=res.detail)

    if res.unknown:
        # Deliberate, and a known limit rather than a decision I am happy
        # with. Refusing here is safer against duplication, but a non-template
        # strategy has no placeholder row to reconcile from, so the signal
        # would stay 'pending' and PendingWatcher would re-activate it every
        # 20s -- the failure that turned 5 signals into ~133 opens on
        # 2026-07-30, which is worse than the duplicate this gate stops.
        # Handling UNKNOWN properly needs the recorded-as-unknown state that
        # stage3/020 introduces.
        log.error(
            "[dedup] could not confirm whether %s is already at the broker (%s) "
            "— sending anyway. See stage3/020.", trade_id[:8], res.detail,
        )
        return _FallbackDecision(send=True, unknown=True, detail=res.detail)

    return _FallbackDecision(send=True, detail=res.detail)

