"""AI Trade Analysis page's API (M3 page drain)."""
from __future__ import annotations

from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.analytics import ai_analysis_repo


def gather_channel_data(db_path: str, days: int) -> list[dict]:
    return ai_analysis_repo._gather_channel_data(db_path, days)


def gather_strategy_dpm_data(db_path: str, days: int) -> dict:
    return ai_analysis_repo._gather_strategy_dpm_data(db_path, days)


def gather_signal_generator_data(db_path: str, days: int) -> dict:
    return ai_analysis_repo._gather_signal_generator_data(db_path, days)


def get_app_config(key: str) -> Optional[str]:
    return db_module.get_app_config(key)


def set_app_config(key: str, value: str) -> None:
    db_module.set_app_config(key, value)
