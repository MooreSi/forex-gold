"""Risk-settings service: the settings surface six pages share.

Wraps `risk_settings_repo` (settings + effective strategy),
`circuit_breaker_repo` (breaker state and reset) and
`custom_strategies_repo` (user-defined strategies), because from a page's
point of view these are one subject: "what is this account allowed to do".

`update` is not a passthrough by accident. `risk_settings_repo.update_risk_settings`
also forwards the change to the paired node over sync and invalidates a 10s
TTL cache; routing every writer through here keeps both from becoming
optional.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.risk import circuit_breaker_repo as _breaker
from backend.src.services.risk import custom_strategies_repo as _custom
from backend.src.services.risk import risk_settings_repo as _repo

__all__ = [
    "get", "get_async", "update",
    "effective_strategy",
    "circuit_breaker_state", "reset_circuit_breaker",
    "custom_strategies", "delete_custom_strategy",
]


def get() -> dict:
    return _repo.get_risk_settings()


async def get_async() -> dict:
    return await to_db_thread(_repo.get_risk_settings)


def update(fields: dict) -> None:
    _repo.update_risk_settings(fields)


def effective_strategy(*args, **kwargs):
    return _repo.get_effective_strategy(*args, **kwargs)


# ── Circuit breaker ──────────────────────────────────────────────────────────

def circuit_breaker_state() -> dict:
    return _breaker.get_circuit_breaker_state()


def reset_circuit_breaker(*args, **kwargs):
    return _breaker.reset_circuit_breaker(*args, **kwargs)


# ── Custom strategies ────────────────────────────────────────────────────────

def custom_strategies(*args, **kwargs):
    return _custom.get_custom_strategies(*args, **kwargs)


def delete_custom_strategy(*args, **kwargs):
    return _custom.delete_custom_strategy(*args, **kwargs)
