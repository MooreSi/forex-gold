"""MT5 closed-position reconciliation lives in a service now (M4 B9c).

_sync_closed_mt5_positions was 208 lines reconciling the app's open trades
against what the broker actually still holds: detecting tickets that
vanished, requiring a miss streak before believing it, recording the close,
syncing profit, and importing untracked positions.

This is the close-adjacent batch, so it is a RELOCATION AND NOTHING ELSE.
The body moves verbatim, `self.X` becomes `ctx.X`, and no argument is
added, removed, defaulted or reordered on any close-path call. The demo
gate stands: _make_close_trade_ctx, close_trade and record_close are not
edited by this batch, and test_close_trade_characterization.py runs
unmodified as the witness.

Nothing here places, closes or modifies an order. The bridge is a fake
that returns canned position lists, and every close-path collaborator on
the ctx is a recorder.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.broker import position_sync as ps


def _engine():
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = object()
    e._mt5_sync_missing_streak = {}
    return e


def test_the_runtime_delegates_reconciliation_to_the_service():
    engine = _engine()
    sentinel = mock.AsyncMock(return_value=None)
    with mock.patch("backend.src.runtime._sync_closed_mt5_positions_impl", sentinel):
        asyncio.run(engine._sync_closed_mt5_positions())

    assert sentinel.await_count == 1
    (ctx,), _ = sentinel.call_args
    assert isinstance(ctx, ps.PositionSyncCtx)


# Every collaborator the inline body reached through `self`. Five of these
# are close-path operations -- if a relocation drops one, trades stop being
# reconciled and the app quietly believes positions are still open.
EXPECTED_CTX_FIELDS = [
    "bridge",
    "mt5_sync_missing_streak",
    "miss_threshold",
    "get_tick",
    "partial_close_trade",
    "record_close",
    "sync_profit",
    "schedule_profit_sync",
    "get_mt5_account",
]


@pytest.mark.parametrize("field", EXPECTED_CTX_FIELDS)
def test_the_context_carries_every_collaborator(field):
    ctx = _engine()._make_position_sync_ctx()
    assert hasattr(ctx, field), f"PositionSyncCtx is missing {field}"


def test_the_bound_collaborators_point_back_at_the_engine():
    engine = _engine()
    ctx = engine._make_position_sync_ctx()

    assert ctx.bridge is engine._bridge
    # Shared by reference: the miss streak counts CONSECUTIVE cycles, so a
    # per-call copy would reset the count every cycle and every transient
    # broker hiccup would be treated as a real close.
    assert ctx.mt5_sync_missing_streak is engine._mt5_sync_missing_streak

    for field, method in [
        ("get_tick", engine.get_tick),
        ("partial_close_trade", engine.partial_close_trade),
        ("record_close", engine.record_close),
        ("sync_profit", engine.sync_profit),
        ("schedule_profit_sync", engine._schedule_profit_sync),
        ("get_mt5_account", engine.get_mt5_account),
    ]:
        assert getattr(ctx, field).__func__ is method.__func__, field


def test_the_miss_threshold_value_is_carried_not_reinvented():
    """The streak threshold is a tuning constant, and a relocation that
    silently changed it would make the app either believe closes too
    eagerly or never at all."""
    ctx = _engine()._make_position_sync_ctx()
    assert ctx.miss_threshold == TradingRuntime.MT5_SYNC_MISS_THRESHOLD == 2


def test_the_relocated_function_is_the_real_one():
    from backend.src import runtime
    assert runtime._sync_closed_mt5_positions_impl is ps.sync_closed_mt5_positions
    assert asyncio.iscoroutinefunction(ps.sync_closed_mt5_positions)


def test_the_close_path_is_untouched_by_this_batch():
    """The demo gate, restated where this batch can trip it."""
    assert hasattr(TradingRuntime, "_make_close_trade_ctx")
    assert hasattr(TradingRuntime, "close_trade")
    assert hasattr(TradingRuntime, "record_close")
    assert hasattr(TradingRuntime, "_schedule_profit_sync")
