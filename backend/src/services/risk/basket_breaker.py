"""Who the circuit breaker counts when several positions close at once.

`docs/todo/bugs/041`. Global Harvest banks a COMBINED total and closes every
position on the symbol — deliberately including legs that are individually
losing, because that is what banking a combined total means. The breaker
counted each leg on its own, so a **profitable** basket pushed the account
toward a halt: an eighty-cent leg inside a +$107.19 basket cost fifteen minutes
of live execution on 2026-09-10.

**The owner's answer (2026-09-11, option B):** a harvest does not count towards
the breaker, and a winning basket still clears the counter.

| the basket | the counter |
|---|---|
| net >= 0 | reset to 0 |
| net < 0 | untouched — no increment, no reset |

and no leg ever scores on its own, whichever way it went.

**Keyed on the broker ticket, not the reason string.** A harvested leg reaches
Python looking exactly like a manual close — the reason is derived from the
deal comment and comes out `MT5_close` either way — so there is nothing in the
close itself to key on. The EA says which tickets it is about to close, before
it closes them, and this holds that until the legs arrive.

**Inert until an EA that sends that message is deployed.** With nothing
registered, `score_close` is exactly the call it replaced.

In memory on purpose: a basket lives for the seconds between the EA's notice
and its last leg being recorded. Surviving a restart would mean a basket
registered by a process that is gone silently swallowing an ordinary close
minutes later.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from backend.src.services.risk.circuit_breaker_repo import (
    get_circuit_breaker_state, record_live_trade_outcome,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
# ticket -> basket id, and basket id -> {"net": float, "open": set[int],
# "scored": bool}. Two maps rather than a scan: the legs arrive one close at a
# time and each one asks "am I in a basket".
_ticket_basket: dict[int, str] = {}
_baskets: dict[str, dict] = {}


def register(basket_id: str, net_pnl: float, tickets: Iterable) -> None:
    """The EA is about to close these tickets as one basket worth `net_pnl`.

    Idempotent: the same basket announced twice is one basket, not two, and
    must not score twice.
    """
    ids = {int(t) for t in tickets if t}
    if not ids:
        return
    with _lock:
        if basket_id in _baskets:
            return
        _baskets[basket_id] = {"net": float(net_pnl), "open": set(ids), "scored": False}
        for t in ids:
            _ticket_basket[t] = basket_id
    log.info("[Basket] %s registered: %d leg(s), net %.2f — the breaker will "
             "score it once, on that net", basket_id, len(ids), net_pnl)


def forget_all() -> None:
    """Drop every registration. For tests and for a clean engine restart."""
    with _lock:
        _ticket_basket.clear()
        _baskets.clear()


def registered_ticket_count() -> int:
    """How many tickets are currently spoken for. For tests and diagnostics."""
    with _lock:
        return len(_ticket_basket)


def basket_net(basket_id: str) -> Optional[float]:
    """The net this basket was registered with, or None if it is not held."""
    with _lock:
        basket = _baskets.get(basket_id)
        return None if basket is None else float(basket["net"])


def score_close(mt5_ticket, won: bool) -> dict:
    """Record one closed live trade against the breaker, and return its state.

    Same signature-in-spirit and identical return as
    `record_live_trade_outcome`, which is what this replaces at
    `trading/close_trade.record_close`'s single call site. A ticket in no
    registered basket takes exactly the old path.
    """
    try:
        ticket = int(mt5_ticket or 0)
    except (TypeError, ValueError):
        ticket = 0

    with _lock:
        basket_id = _ticket_basket.get(ticket)
        basket = _baskets.get(basket_id) if basket_id else None
        if basket is None:
            return record_live_trade_outcome(won=won)
        basket["open"].discard(ticket)
        first = not basket["scored"]
        basket["scored"] = True
        net = basket["net"]
        # Tickets are reused by the broker over time; a registry that kept them
        # would eventually swallow an unrelated close.
        if not basket["open"]:
            _baskets.pop(basket_id, None)
            for t, b in list(_ticket_basket.items()):
                if b == basket_id:
                    _ticket_basket.pop(t, None)

    if not first:
        return _quiet_state()
    if net >= 0:
        log.info("[Basket] %s netted %.2f — clearing the consecutive-loss "
                 "counter and scoring no leg on its own", basket_id, net)
        return record_live_trade_outcome(won=True)
    log.info("[Basket] %s netted %.2f — leaving the consecutive-loss counter "
             "where it was; a basket is an account-level action, not a trade",
             basket_id, net)
    return _quiet_state()


def _quiet_state() -> dict:
    """The breaker's state, stated as "this call did not trip it".

    `record_live_trade_outcome` sets `just_triggered`; `get_circuit_breaker_state`
    does not. Returning the bare state would hand the caller a dict missing a
    key it reads, and the caller is `record_close` -- on the frozen path, where
    a KeyError would be swallowed by its `except` and take the breaker alert
    with it. Same shape in, same shape out.
    """
    state = get_circuit_breaker_state()
    state["just_triggered"] = False
    return state
