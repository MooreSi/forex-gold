"""The Telegram approval poller for the activation screen (bugs/021).

`guard.enforce()` shows the activation screen BEFORE `TradingRuntime.startup()`
and never returns, so on that screen there is no runtime, no engines, and no
`bot_command_loop`. The "new registration request" alert still arrives -- that
is a plain HTTPS send -- but nothing polls `getUpdates`, so pressing Approve
does nothing whatsoever. On the admin machine that is the difference between
being able to license a new client and not.

This is the minimal poller that closes that gap, and what it REFUSES is the
point of it.

The normal panel dispatcher (`core_bot_panel.handle_callback`) routes `buy` and
`sell` to real market orders, and `sys2` to system actions. This loop runs in a
process that is deliberately inert: unlicensed, no runtime, no engines. So it
does not filter the callback data and then hand it to that dispatcher -- it
never imports it. Only two actions exist here, and they are called directly:

    reg_ap   approve a pending registration
    reg_rj   reject one

Anything else is answered ("app is not licensed yet") and dropped. A tap from
any chat other than the configured one is ignored without an answer, since
answering confirms the bot is listening.

This loop's normal end is the runtime's own `bot_command_loop` taking over once
the app is licensed and restarted. Only one process may long-poll a bot token,
so a 409 is expected at that handover and is not an error.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from backend.src.services.positions._panel_shared import CB, SEP, Screen
from backend.src.services.positions._panel_registration import (
    _approve_registration as _approve,
    _reject_registration as _reject,
)

log = logging.getLogger(__name__)

_REFUSAL = "The app is not licensed yet — only registration approval works here."

_POLL_TIMEOUT_S = 10
_CONFLICT_BACKOFF_S = 60
_ERROR_BACKOFF_S = 5


def _get_telegram_config() -> dict:
    from backend.src.services.telegram.repo import get_telegram_config
    return get_telegram_config()


def _get_offset() -> int:
    from backend.src.db import database as db_module
    try:
        return int(db_module.get_app_config("bot_update_offset") or 0)
    except Exception:
        return 0


def _set_offset(value: int) -> None:
    from backend.src.db import database as db_module
    try:
        db_module.set_app_config("bot_update_offset", str(value))
    except Exception as exc:
        log.debug("Activation bot: could not persist offset: %s", exc)


def _default_client_factory():
    import httpx
    return httpx.AsyncClient()


async def _answer(client, token: str, cbq_id: str, text: str) -> None:
    """Acknowledge the tap. Telegram spins the button until this arrives, so an
    unanswered callback looks to the admin like a hang."""
    try:
        await client.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cbq_id, "text": (text or "")[:200]},
            timeout=8,
        )
    except Exception as exc:
        log.debug("Activation bot: answerCallbackQuery failed: %s", exc)


async def _edit(client, token: str, chat_id: str, message_id, screen) -> None:
    """Replace the request message with the outcome, so the admin can see at a
    glance which requests are still outstanding."""
    if screen.mode == "noop" or not message_id:
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json={
                "chat_id":      chat_id,
                "message_id":   message_id,
                "text":         screen.text,
                "parse_mode":   "Markdown",
                "reply_markup": {"inline_keyboard": screen.keyboard or []},
            },
            timeout=8,
        )
    except Exception as exc:
        log.debug("Activation bot: editMessageText failed: %s", exc)


def _dispatch(action: str, args: list) -> Optional[Screen]:
    """The entire action surface of this loop, and its ONLY gate.

    An earlier version also kept an `ALLOWED_ACTIONS` tuple and checked it
    before calling this. Mutation testing showed that constant enforced
    nothing: adding "buy" to it changed no behaviour, because the dispatch
    below still refuses everything it does not name. It was removed rather
    than kept, because a constant that reads as a security control while
    enforcing nothing is the thing someone later trusts.

    Returns None for anything the activation screen must not do.
    """
    if action == "reg_ap" and len(args) >= 2:
        return _approve(args[0], args[1])
    if action == "reg_rj" and args:
        return _reject(args[0])
    return None


async def _handle_callback(client, token: str, cbq: dict,
                           allowed_chat: str) -> None:
    msg        = cbq.get("message") or {}
    chat_id    = str(msg.get("chat", {}).get("id", ""))
    message_id = msg.get("message_id")

    # A callback carries its own chat, so it is checked on its own rather than
    # inherited from anything already trusted. No answer either: answering
    # confirms the bot is listening to a chat not allowed to use it.
    if not chat_id or chat_id != allowed_chat:
        return

    parts  = (cbq.get("data") or "").split(SEP)
    action = parts[1] if len(parts) > 1 and parts[0] == CB else ""

    screen = _dispatch(action, parts[2:])
    if screen is None:
        await _answer(client, token, cbq.get("id"), _REFUSAL)
        return

    await _answer(client, token, cbq.get("id"), screen.toast or "")
    await _edit(client, token, chat_id, message_id, screen)


async def activation_bot_loop(
    is_running: Callable[[], bool],
    client_factory: Optional[Callable[[], Any]] = None,
) -> None:
    """Long-poll getUpdates for registration approvals only.

    Never raises. This runs behind the activation screen, which is the only way
    back into a stranded install; an exception here would take the approval
    path down with it and leave no way to license the machine at all.
    """
    factory = client_factory or _default_client_factory
    client  = factory()
    offset  = _get_offset()
    saved   = offset
    log.info("Activation screen: polling Telegram for registration approvals "
             "only (no other command is served here).")
    try:
        while is_running():
            try:
                cfg          = _get_telegram_config()
                token        = cfg.get("bot_token_enc", "")
                allowed_chat = str(cfg.get("chat_id", ""))
                if not (cfg.get("enabled") and token):
                    await asyncio.sleep(_ERROR_BACKOFF_S)
                    continue

                r = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": _POLL_TIMEOUT_S},
                    timeout=_POLL_TIMEOUT_S + 2,
                )
                if r.status_code == 409:
                    # Another process owns the token. Once the app is licensed
                    # and restarted, that process is the runtime's own loop and
                    # this one is finished.
                    log.info("Activation bot: 409 Conflict — another poller "
                             "owns this token; backing off.")
                    await asyncio.sleep(_CONFLICT_BACKOFF_S)
                    continue
                if r.status_code != 200:
                    await asyncio.sleep(_ERROR_BACKOFF_S)
                    continue

                for update in r.json().get("result", []):
                    uid = update.get("update_id", 0)
                    if uid >= offset:
                        offset = uid + 1
                    cbq = update.get("callback_query")
                    if cbq:
                        await _handle_callback(client, token, cbq, allowed_chat)
                    # A typed message is not served here at all: every slash
                    # command needs the runtime this screen does not have.

                if offset != saved:
                    _set_offset(offset)
                    saved = offset
            except Exception as exc:
                log.warning("Activation bot poll failed: %s", exc)
                await asyncio.sleep(_ERROR_BACKOFF_S)
    finally:
        closer = getattr(client, "aclose", None)
        if closer:
            try:
                await closer()
            except Exception:
                pass
