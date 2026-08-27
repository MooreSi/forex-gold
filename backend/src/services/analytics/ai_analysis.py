"""Data-gathering for the AI Trade Analysis page.

The controller used to call `ai_analysis_repo._gather_channel_data` and its two
siblings directly -- private names, reached across the layer boundary. Renaming
any of them inside the repo would have broken a page. These are the public
service names for the same three reads.
"""
from __future__ import annotations

from backend.src.services.analytics import ai_analysis_repo as _repo

__all__ = ["channel_data", "strategy_dpm_data", "signal_generator_data",
           "signal_generator_system_prompt"]


def channel_data(db_path: str, days: int) -> list[dict]:
    return _repo._gather_channel_data(db_path, days)


def strategy_dpm_data(db_path: str, days: int) -> dict:
    return _repo._gather_strategy_dpm_data(db_path, days)


def signal_generator_data(db_path: str, days: int) -> dict:
    return _repo._gather_signal_generator_data(db_path, days)


def signal_generator_system_prompt() -> str:
    """The system prompt for the signal-generator analysis.

    It has always lived in the repo alongside its JSON schema -- the M3 page
    drain moved it there and never repointed the caller, so the page went on
    naming a `_SIGNAL_GEN_SYSTEM` that was bound nowhere and every click of
    Run Analysis raised NameError (docs/todo/bugs/011).

    Surfaced through the service rather than letting the page reach for the
    repo: the frontend talks to controllers, and controllers do not import a
    service's repo.
    """
    return _repo._SIGNAL_GEN_SYSTEM
