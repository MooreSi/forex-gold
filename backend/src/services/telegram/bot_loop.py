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

from backend.src.config import is_debug as _is_debug
from backend.src.db import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.positions import core_bot_panel as bot_panel
from backend.src.services.telegram.bot_dispatch import PanelCtx as _PanelCtx
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


# ── Control-panel transport (2026-08-26) ──────────────────────────────────────
# Ported from upstream engine.py's _bot_command_loop, whose body this module is.
# core_bot_panel decides WHAT the screen says; these two decide how it reaches
# Telegram. Without them the panel could not render (no inline keyboard on the
# send) and could not respond (a button tap arrives as a callback_query, which
# the loop below simply dropped) -- so /panel did nothing at all.

async def _send_panel_screen(client, token: str, chat_id: str,
                             screen, message_id=None) -> None:
    base = f"https://api.telegram.org/bot{token}"
    try:
        if screen.mode == "noop":
            return
        if screen.mode == "delete" and message_id:
            await client.post(f"{base}/deleteMessage",
                              json={"chat_id": chat_id, "message_id": message_id},
                              timeout=8)
            return
        if screen.mode == "edit" and message_id:
            r = await client.post(f"{base}/editMessageText", json={
                "chat_id":      chat_id,
                "message_id":   message_id,
                "text":         screen.text,
                "parse_mode":   "Markdown",
                "reply_markup": {"inline_keyboard": screen.keyboard or []},
            }, timeout=8)
            # "message is not modified" is the expected response to a tap that
            # changed nothing visible (a stepper already at its floor, say) --
            # not an error worth surfacing.
            if r.status_code != 200 and "not modified" not in r.text:
                log.warning("Panel edit failed: %s", r.text[:200])
            return
        payload = {"chat_id": chat_id, "text": screen.text, "parse_mode": "Markdown"}
        if screen.mode == "force_reply":
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        elif screen.keyboard:
            payload["reply_markup"] = {"inline_keyboard": screen.keyboard}
        r = await client.post(f"{base}/sendMessage", json=payload, timeout=8)
        if r.status_code != 200:
            # Markdown in a DB-sourced value (an underscore in a template name,
            # say) can 400 the whole send; retry as plain text so the user still
            # gets the answer.
            log.warning("Panel send failed (%s), retrying unformatted", r.text[:200])
            payload.pop("parse_mode", None)
            await client.post(f"{base}/sendMessage", json=payload, timeout=8)
    except Exception as e:
        log.warning("Panel transport error: %s", e)


async def _handle_panel_callback(client, token: str, cbq: dict,
                                 allowed_chat: str, ctx) -> None:
    """One inline-button tap."""
    msg        = cbq.get("message") or {}
    chat_id    = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")
    # Same allowlist the message path enforces -- a callback carries its own
    # chat, so it must be checked independently, not inherited.
    if not chat_id or chat_id != allowed_chat:
        return
    screen = await bot_panel.handle_callback(cbq.get("data", ""), ctx)
    # Always answer, even with no toast: Telegram spins on the button until the
    # callback is acknowledged.
    try:
        await client.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cbq.get("id"),
                  "text": (screen.toast or "")[:200]},
            timeout=8,
        )
    except Exception as e:
        log.debug("answerCallbackQuery failed: %s", e)
    await _send_panel_screen(client, token, chat_id, screen, message_id)


async def bot_command_loop(ctx: BotLoopCtx) -> None:
    if _is_debug():
        # Debug mode: no bot token, no outbound polling. The loop simply
        # never starts — everything else about the runtime is unchanged.
        log.info("[debug] telegram bot command loop disabled")
        return
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

                            # An inline-keyboard tap arrives as a
                            # callback_query, not a message -- the panel's
                            # entire navigation and every settings edit comes
                            # through here. Dropped silently before this.
                            cbq = update.get("callback_query")
                            if cbq:
                                await _handle_panel_callback(
                                    _client, token, cbq, allowed_chat,
                                    _PanelCtx(ctx.make_bot_deps()))
                                continue

                            msg     = update.get("message") or {}
                            chat_id = str(msg.get("chat", {}).get("id", ""))
                            text    = (msg.get("text") or "").strip()

                            # Security: only respond to the configured chat
                            if not text or not chat_id or chat_id != allowed_chat:
                                continue

                            # A reply to a panel "Set exact value" prompt.
                            # Checked before the slash-command dispatch because
                            # a typed value is not a command and would
                            # otherwise be dropped.
                            prompt = ((msg.get("reply_to_message") or {}).get("text") or "")
                            if bot_panel.parse_prompt(prompt):
                                screen = await bot_panel.handle_value_reply(prompt, text)
                                await _send_panel_screen(
                                    _client, token, chat_id, screen)
                                continue

                            reply = await _handle_bot_command_impl(
                                text, ctx.make_bot_deps())
                            if isinstance(reply, bot_panel.Screen):
                                await _send_panel_screen(
                                    _client, token, chat_id, reply)
                                continue
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
