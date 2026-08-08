"""Channel-performance service: scorecards, pause state, strategy overrides.

Everything the History page's Channels tab and the Trading page's Strategy AI
panel need, over `channels/repo.py` and `channels/parser_repo.py`.

`strategy_recs` is the one function here that is not a passthrough. The page
used to loop over sources calling `get_channel_strategy_rec` once each, from a
`ui.timer` callback -- a synchronous DB round trip per channel on the event
loop. One `to_db_thread` hop covering the whole loop is the fix, and it has to
live here because the loop is the thing being moved off the loop.
"""
from __future__ import annotations

from typing import Optional

from backend.src.db.database import to_db_thread
from backend.src.services.channels import learned_rules_repo as _rules
from backend.src.services.channels import parser_repo as _parser
from backend.src.services.channels import repo as _repo
from backend.src.services.channels import unrecognised_repo as _unrecognised

__all__ = [
    "scorecard", "recompute", "performance_map", "set_paused",
    "all_strategy_settings", "strategy_rec", "strategy_recs",
    "set_strategy_override",
    "parser_config", "save_parser_config", "save_learned_rule",
    "pending_unrecognised", "update_unrecognised",
]


def scorecard(days: int):
    return _repo.get_channel_scorecard(days)


def recompute(days: int):
    return _repo.recompute_channel_performance(days)


def performance_map():
    return _repo.get_channel_performance_map()


def set_paused(source: str, paused) -> None:
    _repo.set_channel_paused(source, paused)


# ── Strategy recommendations ─────────────────────────────────────────────────

def all_strategy_settings():
    return _repo.get_all_channel_strategy_settings()


def strategy_rec(source: str):
    return _repo.get_channel_strategy_rec(source)


async def strategy_recs(sources: list) -> dict:
    """One off-loop pass over every source.

    A per-channel synchronous read straight from the timer callback stalls the
    event loop once per channel; this batches them into a single hop.
    """
    def _fetch():
        return {src: _repo.get_channel_strategy_rec(src) for src in sources}
    return await to_db_thread(_fetch)


def set_strategy_override(source: str, strategy, auto: bool):
    return _repo.set_channel_strategy_override(source, strategy, auto=auto)


# ── Parsing config + unrecognised messages ───────────────────────────────────

def parser_config(channel_name: str) -> Optional[dict]:
    return _parser.get_channel_parser_config(channel_name)


def save_parser_config(*args, **kwargs):
    return _parser.save_channel_parser_config(*args, **kwargs)


def save_learned_rule(*args, **kwargs):
    return _rules.save_channel_learned_rule(*args, **kwargs)


async def pending_unrecognised(limit: int = 20) -> list[dict]:
    return await to_db_thread(_unrecognised.get_pending_unrecognised_messages, limit=limit)


def update_unrecognised(*args, **kwargs):
    return _unrecognised.update_unrecognised_message(*args, **kwargs)
