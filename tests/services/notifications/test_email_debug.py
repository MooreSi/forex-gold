"""Email is a logged no-op in debug mode (stage2 phase5/020).

send_email must return the success shape without any SMTP/HTTP in debug;
with debug off the provider routing is untouched (negative control).

No test here can send mail: every transport is patched.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from backend.src.services.notifications import email_service


def test_send_noops_in_debug():
    with patch.object(email_service, "_is_debug", return_value=True), \
         patch.object(email_service, "_send_via_resend", new=AsyncMock()) as resend, \
         patch.object(email_service, "_send_via_mailjet", new=AsyncMock()) as mailjet, \
         patch.object(email_service, "_send_sync") as smtp:
        ok, err = asyncio.run(email_service.send_email(
            "subject", "<p>body</p>", {"resend_api_key": "k", "to_addr": "x@y.z"}
        ))
    assert ok is True and err == ""
    resend.assert_not_awaited()
    mailjet.assert_not_awaited()
    smtp.assert_not_called()


def test_send_routes_when_debug_off():
    """Negative control: debug off reaches the provider transport."""
    from backend.src.db import database as db_module

    with patch.object(email_service, "_is_debug", return_value=False), \
         patch.object(email_service, "_send_via_resend",
                      new=AsyncMock(return_value=(True, ""))) as resend, \
         patch.object(db_module, "get_app_config", return_value=None):
        ok, err = asyncio.run(email_service.send_email(
            "subject", "<p>body</p>", {"resend_api_key": "k", "to_addr": "x@y.z"}
        ))
    assert ok is True
    resend.assert_awaited_once()
