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


def save_telegram(*args, **kwargs):
    return _tg_repo.save_telegram_config(*args, **kwargs)
