"""Keep MT5's AutoTrading on, and say so when it is not (owner, 2026-09-28).

The VPS was restarted from its provider's panel on 2026-09-27 and MT5 came back
with AutoTrading off. The bridge watchdog only re-enabled it on a disconnected
-> connected transition; a bridge that is connected from its first check never
makes one. Every order the Mac forwarded from 01:19 until at least 08:24 was
rejected, and the Mac only logged it.

`check_while_connected` runs on each healthy watchdog check. It acts only on
an explicit `trade_allowed: False`: None means the terminal could not be asked,
and a broker that could not be asked has not said no (20-trading-safety.md).
`enable_autotrading` clicks at MT5's window and checks `trade_allowed` before
and after each attempt, so a terminal already on is never toggled off; it is
still tried at most once per AUTOTRADING_RETRY_S. One alert per episode.

`note_order_rejection` alerts when MT5 rejects an order for this reason, at
most once per REJECTION_ALERT_EVERY_S. Nothing here places, closes or modifies
an order. Pinned by tests/broker/test_autotrading_guard.py.
"""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any, Optional

from backend.src.services.telegram import alerts as telegram_alerts

log = logging.getLogger(__name__)

AUTOTRADING_RETRY_S = 300.0
REJECTION_ALERT_EVERY_S = 600.0
_REJECTION_TEXT = "AutoTrading is disabled"

_last_rejection_alert: Optional[float] = None


def _alert(text: str) -> None:
    asyncio.create_task(telegram_alerts.send_message(text))


async def check_while_connected(bridge: Any, health: dict, state: dict, now: float) -> None:
    """One healthy watchdog check. `state` is the watchdog's own dict."""
    allowed = health.get("trade_allowed")
    if allowed is True:
        state["at_episode"] = False
        return
    if allowed is not False:
        return                                  # unknown: never toggle on a guess
    last = state.get("at_last_attempt")
    if last is not None and now - last < AUTOTRADING_RETRY_S:
        return
    state["at_last_attempt"] = now
    try:
        result = await bridge.enable_autotrading()
    except Exception as e:
        result = {"enabled": False, "error": str(e)}
    if result.get("enabled"):
        log.warning("AutoTrading was off in MT5; switched back on (%s)", result.get("method"))
        text = ("*MT5 AutoTrading was off* and has been switched back on. "
                "Orders sent while it was off were rejected.")
    else:
        log.error("AutoTrading is off in MT5 and could not be switched on: %s",
                  result.get("error", "unknown"))
        text = ("*MT5 AutoTrading is OFF* and could not be switched on "
                f"({result.get('error', 'unknown')}). Every order will be rejected "
                "until it is enabled in the MT5 toolbar.")
    if not state.get("at_episode"):
        state["at_episode"] = True
        _alert(text)


def note_order_rejection(error: str) -> None:
    """Alert when MT5 rejected an order because AutoTrading is off."""
    global _last_rejection_alert
    if _REJECTION_TEXT not in str(error or ""):
        return
    now = monotonic()
    if _last_rejection_alert is not None and now - _last_rejection_alert < REJECTION_ALERT_EVERY_S:
        return
    _last_rejection_alert = now
    log.error("An order was rejected because MT5 AutoTrading is off: %s", error)
    _alert("*Order rejected: MT5 AutoTrading is off.* Enable it in the MT5 toolbar; "
           "the watchdog also tries every few minutes.")
