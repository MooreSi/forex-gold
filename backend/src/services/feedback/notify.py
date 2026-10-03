"""Tell the owner a piece of feedback arrived: Telegram and email.

Each channel is tried on its own. One being unconfigured or down must not stop
the other, and neither may stop the entry reaching the console (the caller
stores first).
"""
from __future__ import annotations

import html
import logging
import time

log = logging.getLogger(__name__)

KIND_LABEL = {"feature": "Feature request", "bug": "Bug report",
              "feedback": "General feedback"}


async def _send_telegram(text: str) -> bool:
    from backend.src.services.telegram import alerts
    return await alerts.send_message(text, event_type="feedback")


async def _send_email(subject: str, body: str) -> tuple[bool, str]:
    from backend.src.services.notifications import email_service
    return await email_service.send_email(subject, body)


def _telegram_text(e: dict) -> str:
    from backend.src.services.telegram.alerts import _md_esc
    return (
        f"New {KIND_LABEL.get(e['kind'], 'feedback').lower()}\n"
        f"From: {_md_esc(e.get('name') or '-')} ({_md_esc(e.get('email') or '-')})\n"
        f"Host: {_md_esc(e.get('hostname') or '-')}  v{_md_esc(e.get('version') or '?')}\n\n"
        f"{_md_esc(e['message'])}"
    )


def _email_body(e: dict) -> str:
    esc = lambda v: html.escape(str(v or "-"))            # noqa: E731
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("submitted_at", 0)))
    return (
        f"<h3>{esc(KIND_LABEL.get(e['kind']))}</h3>"
        f"<p><b>From:</b> {esc(e.get('name'))} &lt;{esc(e.get('email'))}&gt;<br>"
        f"<b>Host:</b> {esc(e.get('hostname'))} &nbsp; <b>Version:</b> {esc(e.get('version'))}<br>"
        f"<b>Sent:</b> {esc(when)}</p>"
        f"<p style=\"white-space:pre-wrap\">{esc(e['message'])}</p>"
    )


async def announce(entry: dict) -> None:
    try:
        await _send_telegram(_telegram_text(entry))
    except Exception as exc:                      # noqa: BLE001
        log.warning("[Feedback] Telegram alert failed: %s", exc)
    try:
        ok, why = await _send_email(
            f"{KIND_LABEL.get(entry['kind'], 'Feedback')} from "
            f"{entry.get('name') or entry.get('hostname') or 'a user'}",
            _email_body(entry))
        if not ok:
            log.warning("[Feedback] Email not sent: %s", why)
    except Exception as exc:                      # noqa: BLE001
        log.warning("[Feedback] Email failed: %s", exc)
