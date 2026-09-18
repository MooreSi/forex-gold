"""Parsing tab — what the Telegram reader sees, and the rules it parses by.

The one outbound action in the whole layer lives behind this domain
(`telegram_controller.send_message`) and is deliberately NOT exposed here: the
Parsing tab reads and configures, it does not post.

**The settings section is the part with a history.** This tab shipped from the
2026-08-25 upstream merge with its settings body parked in a function nothing
called. Every switch vanished from the UI while staying fully wired in the
backend, so `immediate_market_entry` could not be turned on and a bare
"Buy Now" signal was missed. Nothing went red, because the render test pinned
the auth wizard instead of the settings. The switches are data now
(`frontend/src/components/parsing/content/settings.ts`) and the tab's test
counts them.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.src.api.deps import reader as reader_dep
from backend.src.controllers import telegram_controller as tg_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/parsing", tags=["parsing"])

# The two numeric settings that sit beside the switches. Clamped to the same
# bounds the page enforced, here rather than in the browser: a value the engine
# would reject must not be accepted and then silently ignored.
MATCH_WINDOW_BOUNDS = (1, 3600)
FALLBACK_SL_BOUNDS = (1.0, 1000.0)


class SettingsUpdate(BaseModel):
    """A partial write. Only the keys present are changed, because the switches
    save one at a time and a whole-object write would race the poll."""
    model_config = {"extra": "allow"}


class LexiconUpdate(BaseModel):
    category: str
    phrases: list[str]


class ChannelParserUpdate(BaseModel):
    channel: str
    enabled: bool


class UnrecognisedResolution(BaseModel):
    row_id: int
    status: str
    channel: Optional[str] = None
    rule: Optional[dict] = None


def _clamp(value: Any, low, high, fallback):
    try:
        return type(low)(max(low, min(high, type(low)(value))))
    except (TypeError, ValueError):
        return fallback


@router.get("/state")
async def state(rdr: Any = Depends(reader_dep)) -> dict:
    """Everything the tab renders, in one read."""
    status = await tg_ctl.get_reader_status(rdr) if rdr is not None else {}
    channels = tg_ctl.get_telegram_channel_names()
    return {
        "reader": status,
        "configured": tg_ctl.reader_is_configured(status),
        "settings": tg_ctl.get_risk_settings(),
        "lexicons": tg_ctl.get_all_lexicons(),
        "lexicon_labels": tg_ctl.LEXICON_LABELS,
        "lexicon_help": tg_ctl.LEXICON_HELP,
        "channels": [
            {"name": name,
             "parser": tg_ctl.get_channel_parser_config(name) or {}}
            for name in channels
        ],
    }


@router.get("/messages")
async def messages(limit: int = Query(100, ge=1, le=500)) -> dict:
    """The stored feed. Separate from /state because it is the big payload and
    the settings above it do not need re-reading to scroll it."""
    rows, total = tg_ctl.fetch_stored_messages(limit)
    return {"messages": rows, "total": total}


@router.get("/unrecognised")
async def unrecognised(limit: int = Query(20, ge=1, le=200)) -> dict:
    """Messages the parser could not read, waiting for someone to say what they
    were."""
    return {"pending": await tg_ctl.get_pending_unrecognised(limit)}


@router.put("/settings")
async def update_settings(body: SettingsUpdate) -> dict:
    """Write one or more parsing switches.

    Clamped here, not in the browser. A match window of 99,999 seconds accepted
    by the UI and rejected by the engine is a setting the operator believes is
    on and that does nothing.
    """
    fields = dict(body.model_dump())
    if "lk_second_message_match_window_sec" in fields:
        fields["lk_second_message_match_window_sec"] = _clamp(
            fields["lk_second_message_match_window_sec"], *MATCH_WINDOW_BOUNDS, 300)
    if "lk_fallback_sl_pips" in fields:
        fields["lk_fallback_sl_pips"] = _clamp(
            fields["lk_fallback_sl_pips"], *FALLBACK_SL_BOUNDS, 50.0)
    tg_ctl.update_risk_settings(fields)
    return tg_ctl.get_risk_settings()


@router.put("/lexicon")
async def set_lexicon(body: LexiconUpdate) -> dict:
    tg_ctl.set_lexicon(body.category, body.phrases)
    return tg_ctl.get_all_lexicons()


@router.put("/channel-parser")
async def set_channel_parser(body: ChannelParserUpdate) -> dict:
    existing = tg_ctl.get_channel_parser_config(body.channel) or {}
    tg_ctl.save_channel_parser_config(body.channel, {**existing, "enabled": body.enabled})
    return tg_ctl.get_channel_parser_config(body.channel) or {}


@router.post("/unrecognised/resolve")
async def resolve_unrecognised(body: UnrecognisedResolution) -> dict:
    """Teach the parser what a message it could not read meant, or dismiss it.

    The rule is saved BEFORE the row is marked resolved: if the order were
    reversed and the write failed, the question would be gone and the rule
    would not exist.
    """
    if body.rule and body.channel:
        tg_ctl.save_channel_learned_rule(body.channel, body.rule)
    tg_ctl.update_unrecognised_message(body.row_id, status=body.status)
    return {"ok": True}
