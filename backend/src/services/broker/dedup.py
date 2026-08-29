"""Has this trade already been placed at the broker?

stage3/010. The hole this closes, from the 2026-08-08 risk review (C1): when
the EA is merely SLOW, `open_trade` times out waiting for its ack, the outer
handler falls back to the Python bridge, and a SECOND order is placed for a
trade the EA may already have on the book. Nothing queried the broker first,
so no send path *could* know.

It was worse than a missing check. The two send paths stamped different
identifiers -- the EA writes "ea:<trade_id[:10]>" on every leg, the bridge
wrote "sig:<signal_id[:8]>" -- so even a check would have had nothing to
match on. Both paths now carry the trade_id, in prefixes that stay distinct so
a bridge order is never mistaken for a template leg by the several services
that parse "ea:" comments.

THREE STATES, NOT TWO. `find_trade` answers found / absent / UNKNOWN, and the
third is the reason this module exists as more than a lookup: a broker that
could not be asked has not said no. Collapsing unknown into absent is exactly
how a retry doubles an order, which is the failure being prevented. Callers
must branch on all three -- see `DedupResult`.

No order is ever placed or closed from here; it only reads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.src.services.broker.ea_bridge import COMMENT_ID_LEN

log = logging.getLogger(__name__)


# Distinct from the EA's "ea:" on purpose. reversal_engine_manage,
# trade_history_repo, template placeholder repair and alerts all parse "ea:"
# comments to map a broker position back onto a template row; a bridge order
# wearing that prefix would be adopted as a leg of a trade it has nothing to
# do with.
BRIDGE_COMMENT_PREFIX = "py:"

# MT5 truncates order comments at 31 characters. "py:" + 10 leaves room to
# spare; a comment cut mid-id would stop matching and silently disable the
# dedup without anything failing.
_MAX_COMMENT_LEN = 31

_DEFAULT_DEAL_DAYS = 1          # the 24h window named in the spec


def comment_for_bridge_order(trade_id: str) -> str:
    """The comment a Python-bridge order carries, so it can be found again."""
    return f"{BRIDGE_COMMENT_PREFIX}{(trade_id or '')[:COMMENT_ID_LEN]}"[:_MAX_COMMENT_LEN]


@dataclass(frozen=True)
class DedupResult:
    """found / absent / unknown.

    `found` and `unknown` are never both true. Both false means a reachable
    broker that does not have this trade -- the only state in which it is safe
    to send.
    """
    found: bool = False
    unknown: bool = False
    ticket: Optional[int] = None
    entry_price: float = 0.0
    by_ea: bool = False       # matched the EA's prefix, not the bridge's
    source: str = ""          # "position" | "deal" | ""
    detail: str = ""

    @property
    def safe_to_send(self) -> bool:
        return not self.found and not self.unknown


def _prefixes(trade_id: str) -> tuple[str, ...]:
    short = (trade_id or "")[:COMMENT_ID_LEN]
    if not short:
        return ()
    return (f"ea:{short}", f"{BRIDGE_COMMENT_PREFIX}{short}")


def _matches(comment: Any, prefixes: tuple[str, ...]) -> Optional[bool]:
    """None when it is not ours; otherwise True if the EA wrote it.

    Which path placed the order decides who manages it, so the answer has to
    carry more than "yes".
    """
    text = str(comment or "")
    for i, p in enumerate(prefixes):
        if text.startswith(p):
            return i == 0          # prefixes[0] is the EA's
    return None


async def find_trade(bridge: Any, trade_id: str,
                     deal_days: int = _DEFAULT_DEAL_DAYS) -> DedupResult:
    """Ask the broker whether `trade_id` already exists there.

    Open positions first, then recent closing history -- a trade can have
    filled AND closed while an ack was outstanding, and checking only open
    positions would report absent and re-send.
    """
    prefixes = _prefixes(trade_id)
    if not prefixes:
        # A blank id would prefix-match every comment and block all trading.
        return DedupResult(detail="no trade id to look for")
    if bridge is None:
        return DedupResult(unknown=True, detail="no bridge available to ask")

    try:
        positions = await bridge.get_positions()
    except Exception as e:
        return DedupResult(unknown=True, detail=f"position query failed: {e}")
    if positions is None:
        # None means "could not look"; [] means "nothing there". Treating them
        # alike is the mistake this whole module exists to prevent.
        return DedupResult(unknown=True, detail="broker returned no position list")

    for p in positions:
        by_ea = _matches(p.get("comment"), prefixes)
        if by_ea is not None:
            return DedupResult(found=True, source="position", by_ea=by_ea,
                               ticket=int(p.get("ticket") or 0) or None,
                               entry_price=float(p.get("open_price") or 0),
                               detail=f"open position {p.get('ticket')}")

    try:
        deals = await bridge.get_deal_history(deal_days)
    except Exception as e:
        return DedupResult(unknown=True, detail=f"deal query failed: {e}")
    if deals is None:
        return DedupResult(unknown=True, detail="broker returned no deal history")

    for d in deals:
        # entry == 0 is an OPENING deal. An exit alone does not prove an order
        # was placed under this id in this attempt.
        if int(d.get("entry", 0) or 0) != 0:
            continue
        by_ea = _matches(d.get("comment"), prefixes)
        if by_ea is not None:
            ticket = int(d.get("order") or d.get("ticket") or 0) or None
            return DedupResult(found=True, source="deal", by_ea=by_ea, ticket=ticket,
                               entry_price=float(d.get("price") or 0),
                               detail=f"opening deal on position {d.get('position_id')}")

    return DedupResult(detail="broker has no record of this trade")
