"""AI Trade Analysis page's API."""
from __future__ import annotations

from typing import Optional

from backend.src.services.analytics import ai_analysis as _analysis
from backend.src.services.risk import app_config as _config

__all__ = ["gather_channel_data", "gather_strategy_dpm_data",
           "gather_signal_generator_data", "get_app_config", "set_app_config"]


def gather_channel_data(db_path: str, days: int) -> list[dict]:
    return _analysis.channel_data(db_path, days)


def gather_strategy_dpm_data(db_path: str, days: int) -> dict:
    return _analysis.strategy_dpm_data(db_path, days)


def gather_signal_generator_data(db_path: str, days: int) -> dict:
    return _analysis.signal_generator_data(db_path, days)


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)
