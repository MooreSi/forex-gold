"""Settings > Connections: the two outbound channels, and proving they work.

Split out of `settings.py` on 2026-09-18, for the reason that file's own
docstring gives — everything that needed a setting was added to one surface
until it reached 3,112 lines. The paths are unchanged (`/api/settings/email`,
`/api/settings/telegram`); only the module they live in moved.

**Everything under `/api/notifications/` sends something.** They are POST for
that reason: a test email or a Telegram alert leaves the machine and cannot be
recalled, and a GET that sends is a GET that a browser prefetch can fire.
Nothing here places an order or touches a position.

Both routers redact through the one `api/redaction.py`, which is where that
denylist moved when this file was split off. Two copies of "never echo a
credential" is one copy that will not be updated when a service grows a new
credential field.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.api.redaction import redacted as _redacted
from backend.src.controllers import notifications_controller as notify_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import telegram_controller as telegram_ctl

log = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


class EmailWrite(BaseModel):
    model_config = {"extra": "allow"}


class TelegramBotWrite(BaseModel):
    """The alert bot's three settings, named as the store names them.

    `bot_token` rather than the column's `bot_token_enc`, because that is what
    the save takes.

    All three default to None and None means "not given". The dashboard saves
    one field as the operator leaves it, so a model that defaulted `enabled` to
    False would switch alerts off every time somebody corrected the chat ID.
    The service is what decides that an omitted value is kept.
    """

    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool | None = None


class TestSend(BaseModel):
    """An optional provider override, so 'Test Delivery' can prove the provider
    the operator just picked rather than the one that is saved."""

    provider: str = ""


_TEST_HTML = (
    '<html><body style="background:#111827;padding:24px;font-family:Arial;">'
    '<p style="color:#f59e0b;font-size:22px;font-weight:bold;">FOREX Trader</p>'
    '<p style="color:#e5e7eb;">{body}</p></body></html>'
)


# ── Email configuration ──────────────────────────────────────────────────────

@router.get("/api/settings/email")
async def email() -> dict:
    return _redacted(settings_ctl.get_email_config())


@router.put("/api/settings/email")
async def save_email(body: EmailWrite) -> dict:
    settings_ctl.save_email_config(dict(body.model_dump()))
    return _redacted(settings_ctl.get_email_config())


# ── Telegram configuration ───────────────────────────────────────────────────

@router.get("/api/settings/telegram")
async def telegram() -> dict:
    return _redacted(settings_ctl.get_telegram_config())


@router.put("/api/settings/telegram")
async def save_telegram(body: TelegramBotWrite) -> dict:
    """Three named values, not the request body.

    Handing the whole body to a three-parameter save is what the React port did
    until 2026-09-18, and no test saw it: every settings test replaced the
    controller with a recorder, and a recorder accepts any shape.
    """
    settings_ctl.save_telegram_config(
        bot_token=body.bot_token, chat_id=body.chat_id, enabled=body.enabled,
    )
    return _redacted(settings_ctl.get_telegram_config())


# ── The Telethon reader's own credentials ────────────────────────────────────

# The reader is a different thing from the alert bot and lives in a different
# store: these three are `config.yaml` keys, read at startup by the Telegram
# reader, while the bot's settings are a database row. Conflating them is what
# the first React port of this tab did, and the result was three fields that
# wrote to a table with no such columns.
READER_KEYS = ("telegram_api_id", "telegram_api_hash", "telegram_phone")


@router.get("/api/settings/telegram-reader")
async def telegram_reader() -> dict:
    """Only the reader's three keys.

    `config.yaml` also holds the AI provider keys and the licence details.
    Answering with the whole file would put those behind one more redaction
    pass than they need to be — the safe shape is to name what is wanted.
    """
    cfg = settings_ctl.load_config() or {}
    return _redacted({k: cfg.get(k, "") for k in READER_KEYS})


@router.put("/api/settings/telegram-reader")
async def save_telegram_reader(body: EmailWrite) -> dict:
    """Write the named keys, and nothing else.

    `save_to_yaml` merges, so an unrecognised key would be written and kept
    for ever. Filtering here rather than trusting the browser is the same rule
    as the redaction going the other way.
    """
    given = {k: v for k, v in dict(body.model_dump()).items() if k in READER_KEYS}
    if not given:
        raise Refusal("Nothing to save.")
    settings_ctl.save_config(given)
    return await telegram_reader()


# ── Proving they work ────────────────────────────────────────────────────────

def _recipient() -> tuple[dict, str]:
    cfg = settings_ctl.get_email_config() or {}
    to_addr = (cfg.get("to_addr") or "").strip()
    if not to_addr:
        raise Refusal("Set a 'Send reports to' address and save it first.")
    return cfg, to_addr


async def _send(subject: str, html: str, cfg: dict, **kwargs) -> dict:
    ok, err = await notify_ctl.send_email(subject, html, cfg, **kwargs)
    if not ok:
        raise Refusal(notify_ctl.friendly_email_error(str(err or "")))
    return {"sent": True, "to": cfg.get("to_addr", "")}


@router.post("/api/notifications/test-email")
async def test_email(body: TestSend) -> dict:
    """Send one message, through the provider the operator is looking at."""
    cfg, to_addr = _recipient()
    if body.provider:
        cfg = {**cfg, "send_provider": body.provider}
    return await _send(
        "FOREX Trader - Delivery Test",
        _TEST_HTML.format(
            body="Delivery test - scheduled reports will be sent this way."),
        cfg,
    )


@router.post("/api/notifications/test-orb-report")
async def test_orb_report() -> dict:
    """The real morning report, built now and sent once.

    A report that cannot be built is reported as such rather than sent empty:
    "no bridge, no candles" and "sent you a blank page" look identical in an
    inbox, and only one of them tells the operator to check MT5.
    """
    cfg, _ = _recipient()
    report = await notify_ctl.build_orb_report()
    if not report:
        raise Refusal(
            "Could not build the report - the MT5 bridge or its candles are "
            "not available right now."
        )
    chart = notify_ctl.build_orb_chart_image(report)
    html = notify_ctl.build_orb_html(
        report, datetime.now().strftime("%A, %d %B %Y"), has_chart=bool(chart),
    )
    return await _send(
        "FOREX Trader - London Open ORB Report (test)", html, cfg,
        image_bytes=chart, image_cid=notify_ctl.ORB_CHART_CID,
    )


@router.post("/api/notifications/test-telegram")
async def test_telegram() -> dict:
    """Uses the SAVED bot settings, deliberately.

    Testing an unsaved token would prove the token and leave the operator with
    a working test and a broken app.
    """
    ok = await telegram_ctl.send_message(
        "*FOREX Trader - Test Alert*\nTelegram alerts are working.",
        event_type="test",
    )
    if not ok:
        raise Refusal(
            "Telegram did not accept it. Check the bot token and chat ID are "
            "saved, and that alerts are enabled."
        )
    return {"sent": True}
