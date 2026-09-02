"""Telegram page's API: plain dicts in, plain dicts out."""
from __future__ import annotations

from typing import Any, Optional

from backend.src.services.channels import performance as _channels
from backend.src.services.risk import settings as _risk
from backend.src.services.telegram import alerts as _alerts
from backend.src.services.telegram import keywords as _keywords
from backend.src.services.telegram import messages as _messages
from backend.src.services.telegram import reader as _reader

__all__ = ["get_risk_settings", "update_risk_settings",
           "get_channel_parser_config", "save_channel_parser_config",
           "save_channel_learned_rule", "update_unrecognised_message",
           "get_reader_status", "get_pending_unrecognised",
           "fetch_stored_messages"]


def get_risk_settings() -> dict:
    return _risk.get()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


def get_channel_parser_config(channel_name: str) -> Optional[dict]:
    return _channels.parser_config(channel_name)


def save_channel_parser_config(*args, **kwargs):
    return _channels.save_parser_config(*args, **kwargs)


def save_channel_learned_rule(*args, **kwargs):
    return _channels.save_learned_rule(*args, **kwargs)


def update_unrecognised_message(*args, **kwargs):
    return _channels.update_unrecognised(*args, **kwargs)


async def get_reader_status(reader: Any) -> dict:
    return await _messages.reader_status(reader)


def reader_is_configured(status: dict) -> bool:
    """Is the Telegram reader set up, as far as a setup checklist cares?

    Here rather than in the view because the view got it wrong: it tested
    `status["connected"] or status["authenticated"]`, and the reader's status
    dict carries neither key -- it reports `auth_state`. Both lookups returned
    None, so the Getting Started row was red on every install however well
    Telegram was configured (owner report, 2026-09-02).

    RECONNECTING counts. A dropped link that is retrying is a configured
    install; telling the user to go and set it up is worse than saying
    nothing.
    """
    from backend.src.services.telegram.reader_common import (
        AUTH_CONNECTED, AUTH_RECONNECTING,
    )
    return status.get("auth_state") in (AUTH_CONNECTED, AUTH_RECONNECTING)


async def get_pending_unrecognised(limit: int = 20) -> list[dict]:
    return await _channels.pending_unrecognised(limit=limit)


def fetch_stored_messages(limit: int = 100) -> tuple[list[dict], int]:
    return _messages.stored(limit)


# ── Outbound alerts ──────────────────────────────────────────────────────────

async def send_message(*args, **kwargs):
    """Post to the configured Telegram chat. OUTBOUND -- this is the only
    function on this module that leaves the machine."""
    return await _alerts.send_message(*args, **kwargs)


# ── Keyword lexicons ─────────────────────────────────────────────────────────

DEFAULT_LEXICONS = _keywords.DEFAULT_LEXICONS
LEXICON_LABELS = _keywords.LEXICON_LABELS
LEXICON_HELP = _keywords.LEXICON_HELP


def get_all_lexicons(*args, **kwargs):
    return _keywords.get_all_lexicons(*args, **kwargs)


def set_lexicon(*args, **kwargs):
    """Rewrite one lexicon. These decide which follow-up messages the parser
    reads as TP hits, stop moves and closes."""
    return _keywords.set_lexicon(*args, **kwargs)


# ── Reader auth states ───────────────────────────────────────────────────────
# The vocabulary the Telegram page's status badge renders. Constants, so the
# page does not import the reader module for six strings.

AUTH_CONNECTED = _reader.AUTH_CONNECTED
AUTH_DISCONNECTED = _reader.AUTH_DISCONNECTED
AUTH_RECONNECTING = _reader.AUTH_RECONNECTING
AUTH_AWAITING_CODE = _reader.AUTH_AWAITING_CODE
AUTH_AWAITING_2FA = _reader.AUTH_AWAITING_2FA
AUTH_FAILED = _reader.AUTH_FAILED


def get_telegram_channel_names(*args, **kwargs):
    """The channel names the schedule page offers per-source rows for.

    The page was importing services.channels.repo for this -- a repo, which is
    a sharper break than reaching a service. Routed through here, and the
    controller reaches the repo through its service, not directly.
    """
    from backend.src.services.channels import performance as _perf
    return _perf.get_telegram_channel_names(*args, **kwargs)
