"""Telegram page's API (M3 page drain): plain dicts in, plain dicts out.

Thin by design -- each function forwards to the same db_module/service
call the page used to make itself, preserving off-loop dispatch where the
page already had it (reader status, unrecognised list). When the
database.py re-export shim dissolves, only this file needs repointing at
the underlying service repos; the page never will.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo


def get_risk_settings() -> dict:
    return db_module.get_risk_settings()


def update_risk_settings(fields: dict) -> None:
    db_module.update_risk_settings(fields)


def get_channel_parser_config(channel_name: str) -> Optional[dict]:
    return db_module.get_channel_parser_config(channel_name)


def save_channel_parser_config(*args, **kwargs):
    return db_module.save_channel_parser_config(*args, **kwargs)


def save_channel_learned_rule(*args, **kwargs):
    return db_module.save_channel_learned_rule(*args, **kwargs)


def update_unrecognised_message(*args, **kwargs):
    return db_module.update_unrecognised_message(*args, **kwargs)


async def get_reader_status(reader: Any) -> dict:
    """reader.get_status() runs a SELECT COUNT(*) -- keep it off the loop."""
    return await db_module.to_db_thread(reader.get_status)


async def get_pending_unrecognised(limit: int = 20) -> list[dict]:
    return await db_module.to_db_thread(
        db_module.get_pending_unrecognised_messages, limit=limit)


def fetch_stored_messages(limit: int = 100) -> tuple[list[dict], int]:
    try:
        return telegram_repo.fetch_stored_messages(limit)
    except Exception:
        return [], 0
