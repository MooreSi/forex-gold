"""Email + Telegram notification config for the Settings page."""
from __future__ import annotations

from backend.src.services.notifications import repo as _repo
from backend.src.services.telegram import repo as _tg_repo

__all__ = ["get_email", "save_email", "get_telegram", "save_telegram"]


def get_email() -> dict:
    return _repo.get_email_config()


def save_email(*args, **kwargs):
    return _repo.save_email_config(*args, **kwargs)


def get_telegram() -> dict:
    return _tg_repo.get_telegram_config()


def save_telegram(
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Store the alert bot's settings. **What is not given is kept.**

    The store rewrites the whole row, so a caller that names one setting has to
    supply the other two or lose them. The dashboard saves a field at a time as
    the operator leaves it, and without this rule changing the chat ID would
    switch alerts off and throw the token away.

    A blank token is the same claim as an absent one. Secrets are write-only
    through the API layer, so the token field is empty on screen whether or not
    one is stored, and passing that empty field through would erase a working
    bot on every edit. Clearing a token deliberately is not offered; `enabled`
    is the switch that stops things being sent.

    The rule lives here rather than in the router because the router forwards
    and does not decide, and because "what is not given is kept" has to be true
    for every caller, not just the browser.
    """
    current = get_telegram() or {}

    token = (bot_token or "").strip() or (current.get("bot_token_enc") or "")
    chat = current.get("chat_id", "") if chat_id is None else chat_id
    on = bool(current.get("enabled", 0)) if enabled is None else bool(enabled)

    _tg_repo.save_telegram_config(token, chat or "", on)
