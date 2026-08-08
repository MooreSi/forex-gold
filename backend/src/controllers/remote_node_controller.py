"""Remote Node page's API: sync-server config and pairing tokens."""
from __future__ import annotations

from typing import Optional

from backend.src.services.cluster import node as _node
from backend.src.services.risk import app_config as _config
from backend.src.services.risk import settings as _risk

__all__ = ["get_app_config", "set_app_config", "get_risk_settings",
           "update_risk_settings", "generate_sync_token", "get_sync_token",
           "restart_app"]


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)


def get_risk_settings() -> dict:
    return _risk.get()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


def generate_sync_token() -> str:
    return _node.generate_sync_token()


def get_sync_token() -> Optional[str]:
    return _node.get_sync_token()


async def restart_app(engine) -> str:
    """Restart the app process so a headless-mode change takes effect.

    The runtime owns the restart -- it holds the bot offset that must be
    persisted first -- so this forwards to it rather than duplicating the
    sequence, and stops the page reaching into `engine._cmd_restart_app`.
    """
    return await engine.restart_app([])
