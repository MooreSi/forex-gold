"""AI Trade Analysis page's API."""
from __future__ import annotations

from typing import Optional

from backend.src.services.analytics import ai_analysis as _analysis
from backend.src.services.risk import app_config as _config

# `signal_generator_system_prompt` is deliberately absent: `system_prompt_for`
# replaced it on 2026-09-19 and is the only way a prompt reaches the model. It
# stays as a function because one test reads it by name.
__all__ = ["gather_channel_data", "gather_strategy_dpm_data",
           "gather_signal_generator_data", "system_prompt_for",
           "get_app_config", "set_app_config"]


def gather_channel_data(db_path: str, days: int) -> list[dict]:
    return _analysis.channel_data(db_path, days)


def gather_strategy_dpm_data(db_path: str, days: int) -> dict:
    return _analysis.strategy_dpm_data(db_path, days)


def gather_signal_generator_data(db_path: str, days: int) -> dict:
    return _analysis.signal_generator_data(db_path, days)


def signal_generator_system_prompt() -> str:
    return _analysis.signal_generator_system_prompt()


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)


def system_prompt_for(subject: str) -> str:
    """The system prompt for one analysis subject. KeyError if there is none.

    Not a fallback: a subject with no prompt of its own must never be sent
    another subject's, because that asks a paid model the wrong question and
    leaves no trace that it happened.
    """
    return _analysis.system_prompt_for(subject)
