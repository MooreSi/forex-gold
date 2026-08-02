"""Trading page's API (M3 page drain): risk settings, custom strategies,
channel-strategy recommendations, signal commentary, and off-loop dispatch
for the page's timer reads. Same calls the page used to make itself.
"""
from __future__ import annotations

import json
from typing import Optional

from backend.src.db import database as db_module


async def run_db(fn, *args, **kwargs):
    """Run a synchronous repo/engine read on the dedicated DB worker thread."""
    return await db_module.to_db_thread(fn, *args, **kwargs)


def get_risk_settings() -> dict:
    return db_module.get_risk_settings()


async def get_risk_settings_async() -> dict:
    return await db_module.to_db_thread(db_module.get_risk_settings)


def update_risk_settings(fields: dict) -> None:
    db_module.update_risk_settings(fields)


def get_app_config(key: str) -> Optional[str]:
    return db_module.get_app_config(key)


def set_app_config(key: str, value: str) -> None:
    db_module.set_app_config(key, value)


def get_circuit_breaker_state() -> dict:
    return db_module.get_circuit_breaker_state()


def get_effective_strategy(*args, **kwargs):
    return db_module.get_effective_strategy(*args, **kwargs)


def get_custom_strategies(*args, **kwargs):
    return db_module.get_custom_strategies(*args, **kwargs)


def delete_custom_strategy(*args, **kwargs):
    return db_module.delete_custom_strategy(*args, **kwargs)


# ── Channel-strategy recommendations (Strategy AI panel) ─────────────────────

def get_all_channel_strategy_settings():
    return db_module.get_all_channel_strategy_settings()


def get_channel_strategy_rec(source: str):
    return db_module.get_channel_strategy_rec(source)


def set_channel_strategy_override(source: str, strategy, auto: bool):
    return db_module.set_channel_strategy_override(source, strategy, auto=auto)


async def get_channel_strategy_recs(sources: list) -> dict:
    """One off-loop pass over all sources -- a per-channel sync DB call
    directly in a timer callback stalls the event loop."""
    def _fetch():
        return {src: db_module.get_channel_strategy_rec(src) for src in sources}
    return await db_module.to_db_thread(_fetch)


# ── Signal commentary + tg-signal row maintenance ────────────────────────────

def get_signal(signal_id: str) -> dict:
    from backend.src.services.signals import repo as signals_repo
    return signals_repo.get_signal(signal_id)


def set_signal_commentary(signal_id: str, commentary: dict) -> None:
    from backend.src.services.signals import repo as signals_repo
    signals_repo.set_signal_commentary(signal_id, json.dumps(commentary))


def delete_tg_signal_row(row_id) -> None:
    from backend.src.services.signals import tg_repo
    tg_repo.delete_tg_signal_row(row_id)
