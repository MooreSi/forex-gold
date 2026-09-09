"""Cancelling a signal, including the broker order it may have placed.

WHY THIS EXISTS
---------------
`runtime.cancel_signal` used to call the signals repo and nothing else, and
that repo does one UPDATE on `vantage_signals`. A signal that had placed a
resting order at the broker was therefore marked cancelled while the order
stayed live. Found running demo 7 on the demo account (2026-09-09):

    vantage_pending_orders  signal_id=4fd3a43f  ea_ticket=1973407311  working
    vantage_signals         signal_id=4fd3a43f  cancelled

The UI said "Signal cancelled" both times.

Since bugs/026 a resting order consumes a trade slot, so the "cancelled" order
went on blocking one until its four-hour expiry -- and it could still fill,
opening a trade the operator believed they had called off. No controller or UI
exposed any other way to withdraw a working order, so once placed it could not
be cancelled from the app at all.

The EA call lives here rather than in the repo because repos hold SQL and
services decide (rules/30-architecture).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from backend.src.services.signals import repo as _repo

log = logging.getLogger(__name__)


def _get_ea():
    from backend.src.services.broker import ea_bridge
    return ea_bridge.get_instance()


def _working_orders_for(signal_id: str) -> list[dict]:
    from backend.src.services.broker import repo as broker_repo
    try:
        rows = broker_repo.fetch_working_pending_orders() or []
    except Exception as exc:
        log.debug("[CancelSignal] could not read working orders: %s", exc)
        return []
    return [r for r in rows if (r or {}).get("signal_id") == signal_id]


def _mark_cancelled(trade_id: str, signal_id: Optional[str]) -> None:
    from backend.src.services.broker import repo as broker_repo
    try:
        broker_repo.apply_pending_cancelled(trade_id, signal_id, time.time())
    except Exception as exc:
        log.debug("[CancelSignal] could not resolve %s: %s", trade_id, exc)


def cancel_signal(signal_id: str) -> None:
    """Cancel the signal AND withdraw any resting order it placed.

    The signal is always cancelled, even when the withdrawal fails: the user
    asked for a cancel, and refusing it because the EA is unreachable would
    leave both the signal and the order live. A withdrawal that the broker
    REFUSES leaves the pending row saying 'working', because it is still
    consuming a slot and can still fill -- marking it resolved there would
    hide a live order.
    """
    _repo.cancel_signal(signal_id)

    orders = _working_orders_for(signal_id)
    if not orders:
        return

    ea = _get_ea()
    if ea is None:
        log.warning(
            "[CancelSignal] %s had %d resting order(s) but no EA is connected "
            "— they are STILL LIVE at the broker",
            signal_id[:8], len(orders),
        )
        return

    for row in orders:
        ticket = int(row.get("ea_ticket") or 0)
        if ticket <= 0:
            # Nothing to withdraw, and guessing a ticket would cancel someone
            # else's order -- the same rule resting_revalidation follows.
            continue
        _withdraw(ea, row, ticket, signal_id)


def _withdraw(ea, row: dict, ticket: int, signal_id: str) -> None:
    trade_id = row.get("trade_id")

    async def _run() -> None:
        try:
            ok = await ea.cancel_pending_order(
                trade_id, ticket, "signal cancelled by the operator")
        except Exception as exc:
            log.warning("[CancelSignal] withdrawing %s ticket=%s failed: %s — "
                        "the order is STILL LIVE", trade_id, ticket, exc)
            return
        if ok:
            _mark_cancelled(trade_id, signal_id)
            log.info("[CancelSignal] withdrew %s ticket=%s with signal %s",
                     trade_id, ticket, signal_id[:8])
        else:
            log.warning("[CancelSignal] the EA refused to withdraw %s ticket=%s "
                        "— it is STILL LIVE and still holds a trade slot",
                        trade_id, ticket)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # Called from a sync context with no loop (tests, CLI): run it now.
        asyncio.run(_run())
