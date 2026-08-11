"""Outbound Telegram is a no-op in debug mode (stage2 phase5/020).

A debug boot must make zero outbound requests; alerts.send_message is the
outbound Telegram boundary. In debug it logs and reports success without
touching HTTP; with debug off the HTTP path is byte-identical (negative
control).

No test here can reach Telegram: httpx is patched at the module boundary
either way.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from backend.src.services.telegram import alerts


def test_send_message_noops_in_debug():
    with patch.object(alerts, "_is_debug", return_value=True), \
         patch.object(alerts, "httpx") as fake_httpx:
        ok = asyncio.run(alerts.send_message("hello", event_type="test"))
    assert ok is True
    fake_httpx.AsyncClient.assert_not_called()


def test_send_message_uses_http_when_debug_off():
    """Negative control: with debug off and a configured bot, the HTTP
    transport IS invoked."""
    response = MagicMock(status_code=200)
    client = MagicMock()
    client.__aenter__ = MagicMock(return_value=client)

    async def _aenter(*a):
        return client

    async def _aexit(*a):
        return False

    async def _post(*a, **k):
        return response

    client.__aenter__ = _aenter
    client.__aexit__ = _aexit
    client.post = _post

    with patch.object(alerts, "_is_debug", return_value=False), \
         patch.object(alerts, "httpx") as fake_httpx, \
         patch.object(alerts.db_module, "get_telegram_config",
                      return_value={"enabled": 1, "bot_token_enc": "t", "chat_id": "c"}), \
         patch.object(alerts.db_module, "log_telegram_event"):
        fake_httpx.AsyncClient = MagicMock(return_value=client)
        ok = asyncio.run(alerts.send_message("hello", event_type="test"))
    assert ok is True
    fake_httpx.AsyncClient.assert_called_once()


def test_bot_command_loop_returns_immediately_in_debug():
    from backend.src.services.telegram import bot_loop

    with patch.object(bot_loop, "_is_debug", return_value=True):
        asyncio.run(bot_loop.bot_command_loop(bot_loop.BotLoopCtx()))
    # reaching here without hanging or raising IS the assertion; the
    # negative control is the guard-position pin below
    import inspect

    src = inspect.getsource(bot_loop.bot_command_loop)
    assert src.index("_is_debug") < src.index("while")
