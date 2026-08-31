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

# ── Lost responses (stage3/020) ──────────────────────────────────────────────

class SendOutcomeUnknown(RuntimeError):
    """The send got no usable answer, so nobody knows whether it filled.

    Deliberately NOT a subclass of the rejection paths. A broker retcode
    saying no is information: nothing filled, and retrying is safe. A timeout,
    a None, or a dead socket is the absence of information, and treating the
    two alike is what puts a possibly-filled signal back in the queue.
    """


# Exception types that mean the request may have reached the broker even
# though no answer came back. asyncio.TimeoutError and TimeoutError are the
# same object on 3.11+, but both names are listed because that is not obvious
# to a reader and the tuple is the thing people will edit.
_NO_ANSWER_ERRORS: tuple[type[BaseException], ...] = (
    SendOutcomeUnknown,
    TimeoutError,
    ConnectionError,
    OSError,          # covers socket-level failures ConnectionError misses
)

# httpx's exceptions inherit from none of the above -- `httpx.ReadTimeout` is
# not a builtin TimeoutError -- so one escaping the bridge client used to be
# read as an ordinary rejection and the signal retried. The client normally
# returns a dict rather than raising (see mt5_client._send_failure), but this
# is the backstop for any path that does not, and for anything added later.
#
# The same never-sent / no-answer split as the client, and for the same reason:
# a connect failure means the bridge is down and nothing was placed, so parking
# there would strand a signal every time it restarts.
try:
    import httpx as _httpx
    _HTTP_NEVER_SENT: tuple[type[BaseException], ...] = (
        _httpx.ConnectError, _httpx.ConnectTimeout, _httpx.PoolTimeout,
    )
    _HTTP_NO_ANSWER: tuple[type[BaseException], ...] = (_httpx.TransportError,)
except Exception:                      # httpx absent: nothing to classify
    _HTTP_NEVER_SENT = ()
    _HTTP_NO_ANSWER = ()


def send_outcome_is_unknown(exc: BaseException) -> bool:
    """True when `exc` means "no answer", not "the broker said no"."""
    if _HTTP_NEVER_SENT and isinstance(exc, _HTTP_NEVER_SENT):
        return False
    if _HTTP_NO_ANSWER and isinstance(exc, _HTTP_NO_ANSWER):
        return True
    return isinstance(exc, _NO_ANSWER_ERRORS)


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
        # An unreachable broker has NOT said the trade is absent, so sending
        # would be a guess. While 010 stood alone this sent anyway, because
        # refusing left the signal 'pending' and PendingWatcher re-activated
        # it every 20s -- the failure that turned 5 signals into ~133 opens on
        # 2026-07-30. stage3/020 added the 'unknown' park, so stopping is now
        # the safe answer rather than the dangerous one.
        log.error(
            "[dedup] could not confirm whether %s is already at the broker (%s) "
            "— REFUSING to send. The signal parks as unknown for reconciliation.",
            trade_id[:8], res.detail,
        )
        raise SendOutcomeUnknown(
            f"could not confirm whether the order was already placed: {res.detail}")

    return _FallbackDecision(send=True, detail=res.detail)
