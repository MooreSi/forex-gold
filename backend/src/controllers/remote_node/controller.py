"""Remote Node page's API (M3 page drain): sync-server config and tokens.
Thin passthroughs to the same db_module calls the page used to make itself.
"""
from __future__ import annotations

from typing import Optional

from backend.src.db import database as db_module


def get_app_config(key: str) -> Optional[str]:
    return db_module.get_app_config(key)


def set_app_config(key: str, value: str) -> None:
    db_module.set_app_config(key, value)


def get_risk_settings() -> dict:
    return db_module.get_risk_settings()


def update_risk_settings(fields: dict) -> None:
    db_module.update_risk_settings(fields)


def generate_sync_token() -> str:
    return db_module.generate_sync_token()


def get_sync_token() -> Optional[str]:
    return db_module.get_sync_token()


async def restart_app(engine) -> str:
    """Restart the app process so a headless-mode change takes effect.

    The page used to reach into the engine's private _cmd_restart_app (M4 B4
    made that leak visible). The runtime still owns the restart -- it holds
    the bot offset that must be persisted first -- so this forwards to it
    rather than duplicating the sequence.
    """
    return await engine._cmd_restart_app([])
