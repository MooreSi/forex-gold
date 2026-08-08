"""Trading page's API: risk settings, custom strategies, channel-strategy
recommendations and signal commentary."""
from __future__ import annotations

from typing import Optional

from backend.src.services.channels import performance as _channels
from backend.src.services.risk import app_config as _config
from backend.src.services.risk import settings as _risk
from backend.src.services.signals import commentary as _commentary
from backend.src.services.trading import engine_reads as _reads

__all__ = [
    "get_risk_settings", "get_risk_settings_async", "update_risk_settings",
    "get_app_config", "set_app_config", "get_circuit_breaker_state",
    "get_effective_strategy", "get_custom_strategies", "delete_custom_strategy",
    "get_all_channel_strategy_settings", "get_channel_strategy_rec",
    "set_channel_strategy_override", "get_channel_strategy_recs",
    "get_signal", "set_signal_commentary", "delete_tg_signal_row",
    "get_open_trades", "get_signals", "get_tg_signals",
]


def get_risk_settings() -> dict:
    return _risk.get()


async def get_risk_settings_async() -> dict:
    return await _risk.get_async()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)


def get_circuit_breaker_state() -> dict:
    return _risk.circuit_breaker_state()


def get_effective_strategy(*args, **kwargs):
    return _risk.effective_strategy(*args, **kwargs)


def get_custom_strategies(*args, **kwargs):
    return _risk.custom_strategies(*args, **kwargs)


def delete_custom_strategy(*args, **kwargs):
    return _risk.delete_custom_strategy(*args, **kwargs)


# -- Channel-strategy recommendations (Strategy AI panel) --------------------

def get_all_channel_strategy_settings():
    return _channels.all_strategy_settings()


def get_channel_strategy_rec(source: str):
    return _channels.strategy_rec(source)


def set_channel_strategy_override(source: str, strategy, auto: bool):
    return _channels.set_strategy_override(source, strategy, auto)


async def get_channel_strategy_recs(sources: list) -> dict:
    return await _channels.strategy_recs(sources)


# -- Signal commentary + tg-signal row maintenance ---------------------------

def get_signal(signal_id: str) -> dict:
    return _commentary.get_signal(signal_id)


def set_signal_commentary(signal_id: str, commentary: dict) -> None:
    _commentary.set_commentary(signal_id, commentary)


def delete_tg_signal_row(row_id) -> None:
    _commentary.delete_tg_row(row_id)


# -- Engine reads (polled from ui.timer callbacks) ---------------------------

async def get_open_trades(engine) -> list[dict]:
    return await _reads.open_trades(engine)


async def get_signals(engine, status=None) -> list[dict]:
    return await _reads.signals(engine, status)


async def get_tg_signals(engine, limit: int = 50) -> list[dict]:
    return await _reads.tg_signals(engine, limit)
