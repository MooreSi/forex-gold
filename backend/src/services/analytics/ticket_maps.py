"""Per-ticket lookup maps for the trade-history table.

Moved out of `controllers/history/controller.py`, where these seven builders
were the bulk of a file whose own docstring claimed "nothing touches the
database". They are not passthroughs and never were: each merges two or three
sources with a defined precedence, which is service work.

**The merge precedence is the behaviour, not an implementation detail.** Each
builder reads the cross-node *consolidated ledger* first and the local repo
second, so local rows overwrite ledger rows for the same ticket. That order is
deliberate -- a ticket this node opened has full local detail, while the ledger
carries only what the peer chose to publish. Reversing it silently degrades
every locally-opened trade to the peer's summary.

**The `except Exception: pass` around each source is also deliberate**, and is
the one place in this refactor where swallow-and-degrade was kept. A node with
no peer configured has no ledger; a fresh install has no ladder-leg table. One
missing source must leave the other sources' entries intact rather than
collapse the whole map to empty and blank out the table.

Off-loop dispatch lives here rather than in the controller: every one of these
runs from a `ui.timer` refresh handler, and a synchronous read there stalls the
event loop (the 400-600ms VPS stalls `to_db_thread` exists to prevent).
"""
from __future__ import annotations

import time
from typing import Optional

from backend.src.db.database import to_db_thread
from backend.src.services.analytics import trade_history_repo as _repo
from backend.src.services.analytics.labels import (
    strategy_display_label, trade_channel_label, trade_source_label,
)
from backend.src.services.cluster import sync_repo as _ledger
from backend.src.services.positions import max_tp_repo as _max_tp

__all__ = [
    "source_map", "strategy_map", "max_tp_map", "rr_map",
    "order_type_map", "group_map", "ticket_info",
]


def _cutoff(days: int) -> float:
    return time.time() - days * 86400


def _label_for(tg_source: str) -> str:
    """Channel name when the trade came from Telegram, else the source label."""
    channel = trade_channel_label(tg_source or "")
    return channel if channel else trade_source_label(tg_source or "")


# ── Builders (synchronous; run on the DB worker thread via the wrappers) ─────

def _source_map(days: int) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        ledger_channels, _, _ = _ledger.get_consolidated_ticket_maps()
        for ticket, tg_source in ledger_channels.items():
            result[ticket] = _label_for(tg_source)
    except Exception:
        pass
    try:
        for mt5_ticket, tg_source in _repo.ticket_sources(_cutoff(days)):
            result[str(mt5_ticket)] = _label_for(tg_source)
    except Exception:
        pass
    try:
        # Adaptive Runner ladder legs 2+ are opened as their own raw MT5
        # tickets, tracked only in vantage_ladder_legs -- they never get
        # their own vantage_simulated_trades row; inherit the real channel
        # from the parent trade instead.
        for mt5_ticket, tg_source in _repo.ticket_sources_for_legs(_cutoff(days)):
            result[str(mt5_ticket)] = _label_for(tg_source)
    except Exception:
        pass
    return result


def _strategy_map(days: int) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        _, ledger_strategies, _ = _ledger.get_consolidated_ticket_maps()
        for ticket, strategy in ledger_strategies.items():
            result[ticket] = strategy_display_label(strategy or "")
    except Exception:
        pass
    try:
        for mt5_ticket, strategy, dpm_trade_id in _repo.ticket_strategies(_cutoff(days)):
            result[str(mt5_ticket)] = (
                "DPM" if dpm_trade_id else strategy_display_label(strategy or ""))
    except Exception:
        pass
    try:
        for mt5_ticket, strategy in _repo.ticket_strategies_for_legs(_cutoff(days)):
            result[str(mt5_ticket)] = strategy_display_label(strategy or "")
    except Exception:
        pass
    return result


def _max_tp_map() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        ledger_max_tp, _ = _ledger.get_consolidated_extra_maps()
        result.update(ledger_max_tp)
    except Exception:
        pass
    try:
        result.update(_max_tp.get_max_tp_map_by_ticket())
    except Exception:
        pass
    return result


def _rr_map() -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        _, ledger_rr = _ledger.get_consolidated_extra_maps()
        result.update(ledger_rr)
    except Exception:
        pass
    try:
        result.update(_max_tp.get_rr_map_by_ticket())
    except Exception:
        pass
    return result


def _order_type_map(days: int) -> dict[str, tuple[str, Optional[float]]]:
    """order_type/pending_placed_at aren't in the consolidated-ledger sync
    protocol yet, so a ticket the OTHER node opened falls back to
    "Market"/no pending time; local-only for now."""
    result: dict[str, tuple[str, Optional[float]]] = {}
    try:
        for mt5_ticket, order_type, pending_placed_at in _repo.ticket_order_types(_cutoff(days)):
            result[str(mt5_ticket)] = (order_type or "market", pending_placed_at)
    except Exception:
        pass
    return result


def _group_map() -> dict[str, tuple[str, int]]:
    """{mt5_ticket_str: (trade_id, tier)} for Adaptive Runner ladder legs,
    used to collapse a signal's N broker-side tickets into one table row."""
    result: dict[str, tuple[str, int]] = {}
    try:
        for trade_id, ticket, tier in _repo.ticket_groups():
            result[str(ticket)] = (trade_id, int(tier))
    except Exception:
        pass
    return result


def _ticket_info() -> dict:
    """{ticket_str: (source_label, strategy_label, direction)} with the same
    cross-node consolidated-ledger fallback as the maps above."""
    info: dict[str, tuple] = {}
    try:
        ledger_channels, ledger_strategies, ledger_directions = \
            _ledger.get_consolidated_ticket_maps()
        for ticket, tg_source in ledger_channels.items():
            info[ticket] = (
                _label_for(tg_source),
                strategy_display_label(ledger_strategies.get(ticket, "") or ""),
                ledger_directions.get(ticket, ""),
            )
    except Exception:
        pass
    try:
        # Local data always wins where both exist.
        for tk, src, strat, dir_ in _repo.all_ticket_info():
            info[str(tk)] = (_label_for(src), strategy_display_label(strat or ""), dir_ or "")
    except Exception:
        pass
    try:
        for tk, src, strat, dir_ in _repo.all_ticket_info_for_legs():
            info[str(tk)] = (_label_for(src), strategy_display_label(strat or ""), dir_ or "")
    except Exception:
        pass
    return info


# ── Public async surface ─────────────────────────────────────────────────────

async def source_map(days: int) -> dict[str, str]:
    return await to_db_thread(_source_map, days)


async def strategy_map(days: int) -> dict[str, str]:
    return await to_db_thread(_strategy_map, days)


async def max_tp_map() -> dict[str, str]:
    return await to_db_thread(_max_tp_map)


async def rr_map() -> dict[str, float]:
    return await to_db_thread(_rr_map)


async def order_type_map(days: int) -> dict[str, tuple[str, Optional[float]]]:
    return await to_db_thread(_order_type_map, days)


async def group_map() -> dict[str, tuple[str, int]]:
    return await to_db_thread(_group_map)


async def ticket_info() -> dict:
    return await to_db_thread(_ticket_info)
