"""Telegram bot command polling loop (M4 B9e).

This was SimulationEngine._bot_command_loop: restore the persisted update
offset, clear any conflicting webhook, then long-poll getUpdates and route
each authorised message through the command dispatcher.

Moved verbatim, including the two things here that are load-bearing and
look like details:

  - ONE pooled httpx client for the loop's lifetime. A fresh AsyncClient
    per ~1s poll meant a TLS handshake per poll, and was the single most
    frequently implicated coroutine in the slow-callback trace.
  - the 409 back-off. Only one process may long-poll a bot token; the
    authority check plus this back-off is what stops a paired Mac/VPS
    install from kicking each other in a loop.

bot_offset is reached through get/set rather than carried as a value: it
advances on every update, is persisted to app_config so a restart does not
re-run commands like /restartapp, and is read by the deps builder too.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

import asyncio
import json
import re

from backend.src.db import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.telegram.bot_dispatch import handle_bot_command as _handle_bot_command_impl


log = logging.getLogger(__name__)


@dataclass
class BotLoopCtx:
    """Everything the polling loop reached for through `self`."""
    is_running: Optional[Callable[[], bool]] = None
    is_bot_command_authority: Optional[Callable[[], bool]] = None
    make_bot_deps: Optional[Callable[[], Any]] = None
    get_bot_offset: Optional[Callable[[], int]] = None
    set_bot_offset: Optional[Callable[[int], None]] = None


async def bot_command_loop(ctx: BotLoopCtx) -> None:
    import httpx
    await asyncio.sleep(15)

    # Restore the update offset persisted from the previous run so we never
    # re-process commands (e.g. /restartapp) that were already handled before
    # the app shut down.
    try:
        saved = db_module.get_app_config("bot_update_offset")
        if saved:
            ctx.set_bot_offset(int(saved))
            log.info("Bot: restored update offset %d from DB", ctx.get_bot_offset())
    except Exception:
        pass

    # One persistent, connection-pooled client for this loop's whole
    # lifetime instead of a fresh httpx.AsyncClient (new TLS handshake)
    # on every ~1s getUpdates poll — that churn was the single most
    # frequently implicated coroutine in LoopMonitor's asyncio-debug
    # slow-callback trace (confirmed live 2026-07-09: thousands of
    # "SimulationEngine._bot_command_loop ... took 0.4-1.5s" entries,
    # far more than any other task). Reusing one client lets httpx keep
    # the connection to api.telegram.org warm across polls.
    _client = httpx.AsyncClient(timeout=12)
    try:
        # Register slash commands and clear any active webhook / conflicting getUpdates
        # session before we start polling. A 409 Conflict on getUpdates always means
        # either a webhook is set or another polling session is still open — deleteWebhook
        # removes both causes in one call. Skipped entirely if this node isn't the
        # current bot-command authority — calling deleteWebhook here would otherwise
        # interrupt the OTHER node's live polling session on every restart of this one.
        if await db_module.to_db_thread(ctx.is_bot_command_authority):
            cfg = db_module.get_telegram_config()
            if cfg.get("enabled") and cfg.get("bot_token_enc"):
                try:
                    await _client.post(
                        f"https://api.telegram.org/bot{cfg['bot_token_enc']}/deleteWebhook",
                        params={"drop_pending_updates": "false"},
                        timeout=10,
                    )
                except Exception:
                    pass
                await telegram_alerts.register_commands(cfg["bot_token_enc"])

        _saved_offset = ctx.get_bot_offset()  # track last-persisted value

        while ctx.is_running():
            try:
                if not await db_module.to_db_thread(ctx.is_bot_command_authority):
                    await asyncio.sleep(5)
                    continue

                cfg          = await db_module.to_db_thread(db_module.get_telegram_config)
                token        = cfg.get("bot_token_enc", "")
                allowed_chat = str(cfg.get("chat_id", ""))

                if cfg.get("enabled") and token:
                    r = await _client.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"offset": ctx.get_bot_offset(), "timeout": 10},
                        timeout=12,
                    )
                    if r.status_code == 200:
                        for update in r.json().get("result", []):
                            uid = update.get("update_id", 0)
                            if uid >= ctx.get_bot_offset():
                                ctx.set_bot_offset(uid + 1)

                            msg     = update.get("message") or {}
                            chat_id = str(msg.get("chat", {}).get("id", ""))
                            text    = (msg.get("text") or "").strip()

                            # Security: only respond to the configured chat
                            if not text or not chat_id or chat_id != allowed_chat:
                                continue

                            reply = await _handle_bot_command_impl(
                                text, ctx.make_bot_deps())
                            if not reply:
                                continue

                            await _client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={
                                    "chat_id":    chat_id,
                                    "text":       reply,
                                    "parse_mode": "Markdown",
                                },
                                timeout=8,
                            )

                        # Persist offset after each successful poll so restarts
                        # resume from the correct position.
                        if ctx.get_bot_offset() != _saved_offset:
                            await db_module.to_db_thread(
                                db_module.set_app_config, "bot_update_offset",
                                str(ctx.get_bot_offset()),
                            )
                            _saved_offset = ctx.get_bot_offset()

                    elif r.status_code == 409:
                        # Another session is polling — back off and retry deleteWebhook
                        log.warning("Bot polling 409 Conflict — clearing webhook and backing off 60s")
                        try:
                            await _client.post(
                                f"https://api.telegram.org/bot{token}/deleteWebhook",
                                params={"drop_pending_updates": "false"},
                                timeout=10,
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(60)
                        continue
            except httpx.TransportError as e:
                # Connection dropped/reset — the pooled client recovers on
                # its own next call; just avoid a busy-loop on repeated failures.
                log.debug("Bot command loop transport error: %s", e)
            except Exception as e:
                log.debug("Bot command loop error: %s", e)
            await asyncio.sleep(1)
    finally:
        await _client.aclose()
