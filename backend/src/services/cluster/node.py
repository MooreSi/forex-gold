"""Node identity: which node trades, and the shared sync token.

Over `cluster/sync_repo.py`. `active_trader` is the Local/Remote switch the app
shell and three pages read; the token is what pairs the two nodes.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.cluster import sync_repo as _repo

__all__ = ["get_active_trader", "set_active_trader",
           "generate_sync_token", "get_sync_token"]


def get_active_trader() -> str:
    return _repo.get_active_trader()


def set_active_trader(value: str, *args, **kwargs):
    return _repo.set_active_trader(value, *args, **kwargs)


def generate_sync_token() -> str:
    return _repo.generate_sync_token()


def get_sync_token() -> Optional[str]:
    return _repo.get_sync_token()
