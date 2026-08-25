"""Trade-history page's API.

This file used to be 403 lines: seven multi-source merge builders, timezone
arithmetic, label lookup and ~25 swallowed exceptions, under a docstring
claiming "nothing touches the database". All of that now lives in
`services/analytics/` -- `ticket_maps` for the merges, `labels` for the
display names, `formatting` for the broker-timestamp handling.

What is left is what a controller is for: naming the operations the page
performs and routing each to one service.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from backend.src.services.analytics import formatting as _fmt
from backend.src.services.analytics import labels as _labels
from backend.src.services.analytics import pnl as _pnl
from backend.src.services.analytics import ticket_maps as _maps
from backend.src.services.broker import fees as _fees
from backend.src.services.channels import performance as _channels
from backend.src.services.positions import spread_cache as _spread
from backend.src.services.risk import app_config as _config

__all__ = [
    "parse_reason", "format_broker_ts", "format_duration", "to_date",
    "broker_ts_to_uk_date", "strategy_display_label", "trade_source_label",
    "trade_channel_label",
    "ticket_source_map", "ticket_strategy_map", "ticket_max_tp_map",
    "ticket_rr_map", "ticket_order_type_map", "ticket_group_map",
    "ticket_info",
    "get_cached_spreads", "cache_spread", "platform_fee_rate",
    "get_hourly_pnl_grid", "session_for_hour",
    "get_app_config", "set_app_config",
    "recompute_channel_performance", "get_channel_scorecard",
    "get_channel_performance_map", "set_channel_paused",
]


# -- Display shaping ---------------------------------------------------------

def parse_reason(comment: str, pnl: float = 0.0) -> str:
    return _labels.parse_reason(comment, pnl)


def strategy_display_label(strategy: str) -> str:
    return _labels.strategy_display_label(strategy)


def trade_source_label(tg_source: str) -> str:
    return _labels.trade_source_label(tg_source)


def trade_channel_label(tg_source: str) -> str:
    return _labels.trade_channel_label(tg_source)


def format_broker_ts(ts) -> str:
    return _fmt.format_broker_ts(ts)


def format_duration(seconds: Optional[float]) -> str:
    return _fmt.format_duration(seconds)


def to_date(ts) -> Optional[date]:
    return _fmt.to_date(ts)


def broker_ts_to_uk_date(ts) -> Optional[date]:
    return _fmt.broker_ts_to_uk_date(ts)


# -- Per-ticket lookup maps --------------------------------------------------

async def ticket_source_map(days: int) -> dict[str, str]:
    return await _maps.source_map(days)


async def ticket_strategy_map(days: int) -> dict[str, str]:
    return await _maps.strategy_map(days)


async def ticket_max_tp_map() -> dict[str, str]:
    return await _maps.max_tp_map()


async def ticket_rr_map() -> dict[str, float]:
    return await _maps.rr_map()


async def ticket_order_type_map(days: int) -> dict[str, tuple[str, Optional[float]]]:
    return await _maps.order_type_map(days)


async def ticket_group_map() -> dict[str, tuple[str, int]]:
    return await _maps.group_map()


async def ticket_info() -> dict:
    return await _maps.ticket_info()


# -- Spreads, P&L, config ----------------------------------------------------

async def get_cached_spreads(tickets: list) -> dict:
    return await _spread.get_cached(tickets)


async def cache_spread(ticket, price, points, cost) -> None:
    return await _spread.cache(ticket, price, points, cost)


async def platform_fee_rate():
    return await _fees.platform_fee_rate()


def get_hourly_pnl_grid(days: int):
    return _pnl.hourly_grid(days)


def session_for_hour(h: int) -> str:
    return _pnl.session_for_hour(h)


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)


# -- Channel performance -----------------------------------------------------

def recompute_channel_performance(days: int):
    return _channels.recompute(days)


def get_channel_scorecard(days: int):
    return _channels.scorecard(days)


def get_channel_performance_map():
    return _channels.performance_map()


def set_channel_paused(source: str, paused) -> None:
    _channels.set_paused(source, paused)


async def template_group_map(leg_comments: dict) -> dict:
    """{ticket: (trade_id, tier)} for EA-template groups of 2+ legs."""
    return await _maps.template_group_map(leg_comments)


async def comment_attribution_maps(leg_comments: dict) -> tuple:
    """(source, strategy, max_tp) maps keyed by ticket, from EA order comments."""
    return await _maps.comment_attribution_maps(leg_comments)
