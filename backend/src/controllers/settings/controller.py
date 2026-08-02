"""Shared app-configuration API for the frontend (M3 page drains): the
get/set surface several pages need -- app_config keys, risk settings, the
active-trader flag. Thin passthroughs to the same db_module calls the pages
used to make themselves; when the database.py re-export shim dissolves,
only this file needs repointing.
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


def get_active_trader() -> str:
    return db_module.get_active_trader()


def set_active_trader(value: str, *args, **kwargs):
    return db_module.set_active_trader(value, *args, **kwargs)
