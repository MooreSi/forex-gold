"""_scan_messages lives in a service now (M4 B9a).

314 lines of Telegram-message pipeline -- dedup, logic keywords, instant
entry, SL adjustment, parse, staleness, strategy resolution, execution,
alerting -- sat inline on the runtime, reachable only by constructing a
SimulationEngine. It moves to services/signals/scan_messages.py verbatim,
with the fifteen `self.X` references it used becoming fields on a ScanCtx
built by the runtime, exactly as _make_close_trade_ctx does for closing.

These tests pin the wiring, not the pipeline: the pipeline's own behaviour
is already covered by the scan-messages characterization packs, which run
unmodified. What can silently break in a relocation is the binding -- a
collaborator dropped from the ctx, or the shell calling something else --
so that is what is asserted here.

No order is placed by anything in this file: the ctx is inspected, and the
one end-to-end call runs against a sentinel service function.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.signals import scan_messages as sm


def _engine():
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = object()
    e._tg_reader = object()
    e._cfg = {"starting_balance": 1000.0}
    e._dpm_candles = None
    e._tg_off_warn_state = {}
    return e


def test_the_runtime_delegates_scanning_to_the_service():
    """The shell calls the relocated function -- nothing else."""
    engine = _engine()
    sentinel = mock.AsyncMock(return_value=[{"tg_message_id": "42"}])
    with mock.patch("backend.src.runtime._scan_messages_impl", sentinel):
        result = asyncio.run(engine._scan_messages())

    assert result == [{"tg_message_id": "42"}]
    assert sentinel.await_count == 1
    (ctx,), _ = sentinel.call_args
    assert isinstance(ctx, sm.ScanCtx)


# Every collaborator the inline body reached for through `self`. If a
# relocation drops one, the pipeline fails at runtime on whichever branch
# happens to need it -- often a rare one. This fails immediately instead.
EXPECTED_CTX_FIELDS = [
    "bridge",
    "tg_reader",
    "cfg",
    "dpm_candles",
    "tg_off_warn_state",
    "engine_for_eval",
    "close_trade",
    "try_ai_signal_fallback",
    "find_and_apply_instant_followup",
    "get_trading_balance",
    "suggest_lot_size",
    "queue_unrecognised",
    "is_trading_paused",
    "get_open_trades",
    "check_pre_trade_filters",
    "open_trade",
]


@pytest.mark.parametrize("field", EXPECTED_CTX_FIELDS)
def test_the_context_carries_every_collaborator_the_inline_body_used(field):
    ctx = _engine()._make_scan_ctx()
    assert hasattr(ctx, field), f"ScanCtx is missing {field}"


def test_the_bound_collaborators_point_back_at_the_engine():
    """A ctx field that is merely present but bound to the wrong object is
    the failure this whole pattern exists to prevent."""
    engine = _engine()
    ctx = engine._make_scan_ctx()

    assert ctx.bridge is engine._bridge
    assert ctx.tg_reader is engine._tg_reader
    assert ctx.cfg is engine._cfg
    assert ctx.engine_for_eval is engine
    # Mutable throttle state must be the engine's own dict, not a copy --
    # the OFF-switch warning is throttled to once per 5 minutes across
    # calls, and a per-call copy would warn on every single scan.
    assert ctx.tg_off_warn_state is engine._tg_off_warn_state

    for field, method in [
        ("close_trade", engine.close_trade),
        ("suggest_lot_size", engine.suggest_lot_size),
        ("get_open_trades", engine.get_open_trades),
        ("open_trade", engine.open_trade),
        ("is_trading_paused", engine.is_trading_paused),
    ]:
        assert getattr(ctx, field).__func__ is method.__func__, field


def test_the_relocated_function_is_the_real_one():
    """Negative control: the shell must not be delegating to a stub."""
    assert asyncio.iscoroutinefunction(sm.scan_messages)
    from backend.src import runtime
    assert runtime._scan_messages_impl is sm.scan_messages
