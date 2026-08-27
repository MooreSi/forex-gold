"""The EA leg-id and order-comment vocabulary.

Python keeps ONE vantage_simulated_trades row per template trade, so the leg
suffix on a trade_id and the comment the EA stamps on each broker order are
the only links from an individual leg back to that row.

Its own module because both halves of EABridge need it and so do four other
services (reversal_engine_manage, trade_history_repo, template placeholder
repair, alerts). Importing it from the package __init__ would make the events
mixin import the package that imports it.

Everything here is re-exported from the package, so
`from backend.src.services.broker.ea_bridge import comment_for_trade` keeps
working unchanged.
"""
from __future__ import annotations

import re
from typing import Optional


# Python keeps ONE vantage_simulated_trades row per template trade, so every
# inbound leg event has to be mapped back onto that row (see
# EABridge._resolve_leg_event). Anchor-leg events used to have no mapping at
# all: their unsolicited "trade_opened" matched no open_trade() ack callback
# and was dropped, so the placeholder row kept mt5_ticket=0/entry_price=0
# forever and every later tp_hit/sl_moved/trade_closed for the leg was logged
# as "unknown trade_id" and discarded (confirmed live 2026-07-29 on
# 76687f1a/e93f3fe7/c2ebb432).
_LEG_ID_RE = re.compile(r"^(?P<base>.+)-(?P<kind>[ag])(?P<num>\d+)$")

_LEG_KIND_LABELS = {"a": "Anchor Leg", "g": "Grid Leg"}

def split_leg_trade_id(trade_id: str) -> tuple[str, Optional[str], str]:
    """Split an EA leg trade_id into (base_trade_id, kind, leg_num).

    kind is "a" (anchor), "g" (grid), or None when the id carries no leg
    suffix at all -- in which case base_trade_id is the id unchanged."""
    m = _LEG_ID_RE.match(trade_id or "")
    if not m:
        return (trade_id, None, "")
    return (m.group("base"), m.group("kind"), m.group("num"))


def leg_label(kind: Optional[str], num: str) -> str:
    """Human label for a leg suffix, e.g. ("g", "2") -> "Grid Leg 2"."""
    return f"{_LEG_KIND_LABELS.get(kind or '', 'Leg')} {num}".strip()


# The order comment the EA stamps on every template leg:
#   "ea:" + StringSubstr(trade_id, 0, 10) + <a|g> + <N>
# It is the ONLY link from a broker position back to the app's trade_id that
# survives into MT5's own position and deal records, and for every leg except
# the one that promoted the row it is the only link that exists at all --
# Python keeps one vantage_simulated_trades row per template trade, so sibling
# legs have no row and no ticket of their own on this side.
COMMENT_PREFIX = "ea:"
COMMENT_ID_LEN = 10


def comment_for_trade(trade_id: str) -> str:
    """The comment prefix every leg of `trade_id` will carry."""
    return f"{COMMENT_PREFIX}{(trade_id or '')[:COMMENT_ID_LEN]}"


def trade_id_prefix_from_comment(comment: str) -> Optional[str]:
    """Recover a trade_id's leading characters from a leg's order comment.

    "ea:5b88a61e-6g3" -> "5b88a61e-6". Returns None for any comment the EA
    did not write (broker-generated "[sl 4046.50]", "batchClose", blanks).
    Match the result with a prefix comparison, not equality -- it is only the
    first COMMENT_ID_LEN characters of the full trade_id.
    """
    if not comment or not comment.startswith(COMMENT_PREFIX):
        return None
    # The EA always writes exactly COMMENT_ID_LEN id characters before the leg
    # marker, so a slice is unambiguous where pattern-matching the marker off
    # the end is not: "ea:f4ef1085-aa1" is the id "f4ef1085-a" plus leg "a1",
    # and no regex can tell that from a shorter id without knowing the length.
    ident = comment[len(COMMENT_PREFIX):][:COMMENT_ID_LEN].strip()
    return ident or None


