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
    "parser_config", "save_parser_config", "set_parser_enabled",
    "save_learned_rule",
    "pending_unrecognised", "update_unrecognised",
]


def scorecard(days: int):
    return _repo.get_channel_scorecard(days)


def recompute(days: int):
    return _repo.recompute_channel_performance(days)


def performance_map():
    # scorecard_repo, not repo: the display-only accessor moved there
    # 2026-09-03 to keep repo.py under its line ceiling.
    from . import scorecard_repo as _scorecard
    return _scorecard.get_channel_performance_map()


def set_paused(source: str, paused) -> None:
    _repo.set_channel_paused(source, paused)


# ── Strategy recommendations ─────────────────────────────────────────────────

def all_strategy_settings():
    return _repo.get_all_channel_strategy_settings()


def strategy_rec(source: str):
    return _repo.get_channel_strategy_rec(source)


def strategy_rec_map(sources: list) -> dict:
    """{source: recommendation} for a handful of channels, synchronously.

    `strategy_recs` above is the one to use on a request path -- it batches
    off the event loop. This exists for the AI Analysis page, which builds its
    output inside a synchronous render and only ever holds a few channels.

    Here rather than in the controller: a controller routes, it does not loop.
    Putting this comprehension there took trading_controller.py to 206 lines
    and the controller-LOC gate caught it, correctly.
    """
    return {src: strategy_rec(src) for src in sources if src}


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


def set_parser_enabled(channel_name: str, enabled: bool) -> dict:
    """Turn one channel's parser on or off, carrying the rest of its row.

    Here rather than in the router, which had been calling the repo's
    six-argument writer with two -- a TypeError on every click, so the Parsing
    -> Channels checkboxes did nothing at all until 2026-09-21.

    **The other five columns are read back and re-written, not defaulted.**
    `parser_format` decides how that channel's messages are READ; resetting it
    while switching the channel off would be a silent change to a parsing rule
    behind a checkbox that claims to do one thing.

    A channel with no row yet gets one: the list on that tab is built from
    stored messages, so a channel can be visible and switchable before anything
    has ever configured it.
    """
    existing = _parser.get_channel_parser_config(channel_name) or {}
    _parser.save_channel_parser_config(
        channel_name,
        str(existing.get("parser_format") or ""),
        str(existing.get("signal_prefix") or ""),
        bool(existing.get("instant_entry_enabled")),
        bool(enabled),
        str(existing.get("notes") or ""),
    )
    return _parser.get_channel_parser_config(channel_name) or {}


def save_learned_rule(*args, **kwargs):
    return _rules.save_channel_learned_rule(*args, **kwargs)


async def pending_unrecognised(limit: int = 20) -> list[dict]:
    return await to_db_thread(_unrecognised.get_pending_unrecognised_messages, limit=limit)


def update_unrecognised(*args, **kwargs):
    return _unrecognised.update_unrecognised_message(*args, **kwargs)


def get_telegram_channel_names() -> list[str]:
    """Distinct channel names seen in stored Telegram messages.

    Service-level wrapper so the schedule page can reach this through a
    controller. The page was importing channels.repo directly, and pointing a
    controller at the repo instead would only move the break -- controllers may
    not import a repo either.
    """
    from backend.src.services.channels import repo as _repo
    return _repo.get_telegram_channel_names()
