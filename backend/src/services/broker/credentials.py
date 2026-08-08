"""MT5 credential storage for the Settings page."""
from __future__ import annotations

from backend.src.services.broker import credentials_repo as _repo

__all__ = ["get_mt5", "save_mt5", "sync_bridge_file"]


def get_mt5(*args, **kwargs):
    return _repo.get_mt5_credentials(*args, **kwargs)


def save_mt5(*args, **kwargs):
    return _repo.save_mt5_credentials(*args, **kwargs)


def sync_bridge_file(*args, **kwargs):
    return _repo.sync_bridge_credentials_file(*args, **kwargs)
