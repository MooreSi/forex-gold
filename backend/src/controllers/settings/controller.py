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


# ── Settings page surface (M3 page drain) ────────────────────────────────────

async def run_db(fn, *args, **kwargs):
    """Off-loop dispatch for the page's slow reads (log scan, diagnostics)."""
    return await db_module.to_db_thread(fn, *args, **kwargs)


def get_email_config() -> dict:
    return db_module.get_email_config()


def save_email_config(*args, **kwargs):
    return db_module.save_email_config(*args, **kwargs)


def get_telegram_config() -> dict:
    return db_module.get_telegram_config()


def save_telegram_config(*args, **kwargs):
    return db_module.save_telegram_config(*args, **kwargs)


def get_mt5_credentials(*args, **kwargs):
    return db_module.get_mt5_credentials(*args, **kwargs)


def save_mt5_credentials(*args, **kwargs):
    return db_module.save_mt5_credentials(*args, **kwargs)


def sync_bridge_credentials_file(*args, **kwargs):
    return db_module.sync_bridge_credentials_file(*args, **kwargs)


def get_data_retention_days() -> int:
    return db_module.get_data_retention_days()


def set_data_retention_days(days: int) -> None:
    db_module.set_data_retention_days(days)


def reset_circuit_breaker(*args, **kwargs):
    return db_module.reset_circuit_breaker(*args, **kwargs)


def fetch_signal_execution_lags(db_path: str) -> list:
    from backend.src.services.analytics import read_repo
    return read_repo.fetch_signal_execution_lags(db_path)


# ── App-shell surface (M3 drain of frontend/app.py) ──────────────────────────

def get_circuit_breaker_state() -> dict:
    return db_module.get_circuit_breaker_state()


def switch_environment_db(db_path: str) -> None:
    """Re-point the shared connection at another environment's DB file --
    init() closes stale connections and flushes every registered cache."""
    db_module.init(db_path)


def fetch_realised_pnl_last_24h(cutoff: float) -> float:
    from backend.src.services.analytics import read_repo
    return read_repo.fetch_realised_pnl_last_24h(cutoff)
