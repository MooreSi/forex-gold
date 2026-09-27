"""The self-heal Telegram alert survives Telegram's Markdown parser.

2026-09-26 15:12:14, Mac: "Telegram rejected the markup on a - alert ...
Can't find end of the entity". The alert read "Self-heal: bridge_offline
detected." -- and in Telegram Markdown v1 the underscore opens an italic span
that never closes. alerts.py resent it as plain text, so it arrived, but only
after a 400 and a second request.

Nothing is sent: alerts.send_message and the email service are stubs.
"""
from __future__ import annotations

import asyncio

from backend.src.services.health import self_healer as sh
from backend.src.services.notifications import email_service
from backend.src.services.telegram import alerts


def test_the_condition_and_action_are_escaped(monkeypatch):
    sent: list[str] = []

    async def _send(text, *a, **k):
        sent.append(text)
        return True

    async def _email(*a, **k):
        return True, None

    monkeypatch.setattr(alerts, "send_message", _send)
    monkeypatch.setattr(email_service, "send_email", _email)

    asyncio.run(sh._send_heal_notification("bridge_offline", "restarted mt5_bridge"))

    assert sent
    assert "bridge\\_offline" in sent[0]
    assert "mt5\\_bridge" in sent[0]
