"""Refuse a `template:<name>` order this node has no template for (owner, 2026-09-29).

The VPS's `ea_trade_templates` table was empty while its channels and the
Reversal Engine traded `template:30 TP1 SL50 and Trail`. `open_trade` looked
the template up, got None, and sent the order to the EA anyway with no
template: 38 trades ran with no partial-close ladder, 18 of them through the
template's TP1 without closing a lot. Owner: "refuse missing-template orders".

A missing template is refused the way a stale EA build is
(`template_blocked_by_stale_build`): there is no Python fallback for a
template, so an order the EA cannot manage by it is not a trade this app
knows how to run. A library that cannot be read refuses too: `open_trade`
could not load the template either.

The refusal alerts on Telegram, at most once per template per 30 minutes: the
engine re-signals often, and each refusal is the same fault.
Pinned by tests/trading/test_missing_template_refuses_the_order.py.
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.src.services.broker import ea_templates

log = logging.getLogger(__name__)

_ALERT_EVERY_S = 1800.0
_last_alert: dict[str, float] = {}


def missing_template_reason(strategy) -> str | None:
    """Why this order must not open, or None when it may."""
    if not ea_templates.is_template_override(strategy):
        return None
    name = ea_templates.template_name_from_override(strategy)
    try:
        if ea_templates.get_ea_template(name) is not None:
            return None
        why = "this node has no such template"
    except Exception as e:
        why = f"the template library could not be read ({e})"
    reason = (f"EA Template refused: {strategy!r} -- {why}. Without it the EA "
              f"would run the trade with no TP ladder or partial closes. "
              f"On a VPS, templates arrive from the paired Mac over the sync link.")
    log.error("[EATemplates] %s", reason)
    _alert_once(name, reason)
    return reason


def _alert_once(name: str, reason: str) -> None:
    now = time.monotonic()
    if now - _last_alert.get(name, -_ALERT_EVERY_S) < _ALERT_EVERY_S:
        return
    _last_alert[name] = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from backend.src.services.telegram import alerts as telegram_alerts

    async def _send():
        try:
            await telegram_alerts.send_message(f"*Order refused*\n{reason}",
                                               event_type="trade_error")
        except Exception as e:
            log.warning("[EATemplates] could not send the refusal alert: %s", e)
    loop.create_task(_send())
